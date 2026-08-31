"""Risk rules: stop placement, target placement, the R:R floor, and sizing.

The 1:4 minimum risk-to-reward from the README lives here. Two things about it
are deliberate and should not be "helpfully" relaxed:

1. A setup that cannot reach 4R at a sane target is **rejected**, not shrunk to
   fit. Moving a stop closer to manufacture the ratio just converts a losing
   trade into a losing trade that stops out sooner.
2. The floor is checked against the *net* ratio, after spread, commission and
   slippage. A 4.0 gross setup that nets 3.6 is not a 1:4 trade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .errors import RiskError
from .instruments import Instrument, pips_between, price_from_pips, round_price
from .models import Direction


@dataclass(frozen=True)
class StopTarget:
    """A validated stop/target pair with its cost-adjusted ratio."""

    entry: float
    stop_loss: float
    take_profit: float
    risk_pips: float
    reward_pips: float
    risk_reward: float
    gross_risk_reward: float

    @property
    def is_valid(self) -> bool:
        return self.risk_pips > 0 and self.reward_pips > 0


@dataclass(frozen=True)
class PositionSize:
    """How much to trade, and what it costs if the stop is hit."""

    units: float
    lots: float
    risk_amount: float
    pip_value_per_lot: float
    approximate: bool = False

    @property
    def note(self) -> str:
        return (
            "pip value approximated: needs a cross rate for this pair"
            if self.approximate
            else ""
        )


def stop_bounds(instrument: Instrument, config: Config) -> tuple[float, float]:
    """The pip window a stop may sit in, normalised for this instrument.

    The configured bounds are written in major-pair pips. Gold's pip is twenty
    times smaller relative to its price, so applying the same number to it would
    clamp every gold stop into a fraction of one bar. ``Instrument.stop_scale``
    converts the window; it is 1.0 for every pair quoted like a major, so their
    behaviour is unchanged.
    """
    scale = instrument.stop_scale
    return config.risk.min_stop_pips * scale, config.risk.max_stop_pips * scale


def structural_stop(
    direction: Direction,
    entry: float,
    structure_level: float,
    atr_value: float,
    instrument: Instrument,
    config: Config,
) -> float:
    """Place a stop beyond the structure that invalidates the idea.

    The buffer matters: a stop sitting exactly on a swing low is the price every
    other participant can see, and it gets swept. We push it a fraction of ATR
    past the level, then clamp the resulting distance to the configured pip
    bounds so a single volatile bar cannot produce an absurd stop.
    """
    buffer = atr_value * config.risk.structure_buffer_atr
    raw = structure_level - buffer if direction is Direction.LONG else structure_level + buffer

    distance_pips = pips_between(instrument, entry, raw)
    floor, ceiling = stop_bounds(instrument, config)
    clamped = min(max(distance_pips, floor), ceiling)

    if not math.isclose(clamped, distance_pips, rel_tol=1e-9):
        offset = price_from_pips(instrument, clamped)
        raw = entry - offset if direction is Direction.LONG else entry + offset

    return round_price(instrument, raw)


def atr_stop(
    direction: Direction, entry: float, atr_value: float, instrument: Instrument, config: Config
) -> float:
    """Fallback stop for when no clean structure level is available."""
    offset = atr_value * config.risk.stop_atr_multiple
    floor, ceiling = stop_bounds(instrument, config)
    distance_pips = min(max(offset / instrument.pip_size, floor), ceiling)
    price_offset = price_from_pips(instrument, distance_pips)
    raw = entry - price_offset if direction is Direction.LONG else entry + price_offset
    return round_price(instrument, raw)


def total_cost_pips(instrument: Instrument, config: Config) -> float:
    """Round-trip friction in pips: spread plus slippage.

    Commission is handled separately in cash terms because it does not scale
    with the stop distance.
    """
    spread = (
        config.backtest.spread_pips
        if config.backtest.spread_pips is not None
        else instrument.typical_spread_pips
    )
    return spread + config.backtest.slippage_pips


def build_stop_target(
    direction: Direction,
    entry: float,
    stop_loss: float,
    instrument: Instrument,
    config: Config,
    target_level: float | None = None,
) -> StopTarget:
    """Derive the take profit and check the ratio against the floor.

    If ``target_level`` (a structural objective such as the prior swing) is given
    and it sits *beyond* the minimum-R:R distance, we use it — taking the extra
    reward the market is offering. If it falls short, we do not shorten the
    target to reach it; we place the target at the floor distance instead and let
    the caller decide whether that level is realistic.
    """
    sign = direction.sign
    risk_price = (entry - stop_loss) * sign
    if risk_price <= 0:
        raise RiskError(
            f"stop loss {stop_loss} is on the wrong side of entry {entry} for a "
            f"{direction.value} trade"
        )

    risk_pips = pips_between(instrument, entry, stop_loss)
    cost = total_cost_pips(instrument, config)
    # Costs widen the effective risk and eat into the reward, so charge them once
    # to each side rather than pretending they are free.
    net_risk_pips = risk_pips + cost
    required_reward_pips = net_risk_pips * config.risk.min_risk_reward + cost

    floor_target_price = entry + sign * price_from_pips(instrument, required_reward_pips)

    if target_level is not None:
        beyond = (target_level - entry) * sign
        if beyond > (floor_target_price - entry) * sign:
            take_profit = target_level
        else:
            take_profit = floor_target_price
    else:
        take_profit = floor_target_price

    take_profit = round_price(instrument, take_profit)
    reward_pips = pips_between(instrument, entry, take_profit)

    net_reward_pips = max(reward_pips - cost, 0.0)
    risk_reward = net_reward_pips / net_risk_pips if net_risk_pips > 0 else 0.0
    gross_rr = reward_pips / risk_pips if risk_pips > 0 else 0.0

    return StopTarget(
        entry=round_price(instrument, entry),
        stop_loss=round_price(instrument, stop_loss),
        take_profit=take_profit,
        risk_pips=round(risk_pips, 2),
        reward_pips=round(reward_pips, 2),
        risk_reward=round(risk_reward, 3),
        gross_risk_reward=round(gross_rr, 3),
    )


def enforce_rr(setup: StopTarget, config: Config) -> None:
    """Raise unless the net ratio clears the configured floor.

    Called by ``signals.build_signal`` on every signal. A tiny tolerance absorbs
    float rounding at the boundary; it does not permit a genuinely short trade.
    """
    floor = config.risk.min_risk_reward
    if setup.risk_reward + 1e-9 < floor:
        raise RiskError(
            f"risk-to-reward {setup.risk_reward:.2f} is below the {floor:.1f} floor "
            f"(risk {setup.risk_pips:.1f} pips, reward {setup.reward_pips:.1f} pips "
            f"after costs). Rejected rather than re-cut to fit."
        )
    if setup.risk_reward > config.risk.max_risk_reward:
        raise RiskError(
            f"risk-to-reward {setup.risk_reward:.2f} exceeds the {config.risk.max_risk_reward:.1f} "
            f"ceiling; a target that far away is unlikely to fill and would flatter the backtest."
        )


def pip_value_per_lot(
    instrument: Instrument, price: float, account_currency: str = "USD"
) -> tuple[float, bool]:
    """Value of one pip for one standard lot, in the account currency.

    Returns ``(value, approximate)``. The flag is set when the pair shares no
    currency with the account and a cross rate we do not have would be needed —
    the number is then a usable estimate, and the signal card says so rather than
    presenting a guess as fact.
    """
    account = account_currency.upper()
    pip_in_quote = instrument.pip_size * instrument.contract_size

    if instrument.quote == account:
        # e.g. EURUSD with a USD account: one pip is a fixed $10 per lot.
        return pip_in_quote, False
    if instrument.base == account:
        # e.g. USDJPY with a USD account: convert the quote-currency pip at price.
        if price <= 0:
            raise RiskError("cannot compute pip value from a non-positive price")
        return pip_in_quote / price, False
    # Cross pair against the account currency: assume near-parity and flag it.
    return pip_in_quote, True


def position_size(
    instrument: Instrument, entry: float, stop_loss: float, config: Config
) -> PositionSize:
    """Size the trade so that a stop-out costs exactly the configured risk.

    Sizing is derived from the stop distance, never the other way round. A wider
    stop buys a smaller position, not a larger loss.
    """
    risk_pips = pips_between(instrument, entry, stop_loss)
    if risk_pips <= 0:
        raise RiskError("cannot size a position with a zero-pip stop")

    risk_amount = config.account.balance * (config.account.risk_per_trade_pct / 100.0)
    per_lot, approximate = pip_value_per_lot(instrument, entry, config.account.currency)
    if per_lot <= 0:
        raise RiskError(f"computed a non-positive pip value for {instrument.symbol}")

    lots = risk_amount / (risk_pips * per_lot)
    # Round down to micro lots: rounding up would breach the risk cap.
    lots = math.floor(lots * 100) / 100

    return PositionSize(
        units=round(lots * instrument.contract_size, 2),
        lots=lots,
        risk_amount=round(lots * risk_pips * per_lot, 2),
        pip_value_per_lot=round(per_lot, 4),
        approximate=approximate,
    )
