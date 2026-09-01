"""Bar-by-bar backtesting.

The purpose of this module is not to produce a good-looking equity curve. It is
to produce a win rate the user can actually reproduce with a broker. Every
modelling decision below is therefore taken pessimistically, and the reasons are
written down so that nobody "fixes" them later:

* **Signals fire on a closed bar; fills happen on the next bar's open.** You
  cannot trade a bar you are still inside. Entering at the signal bar's close is
  the most common way a backtest invents edge that does not exist.
* **When a bar touches both the stop and the target, the stop wins.** From OHLC
  alone the order of the two touches is unknowable. For a 1:4 system this single
  assumption can swing the reported win rate by tens of points, so it is resolved
  against us, always.
* **Gaps fill at the open, not the level.** If price opens through your stop, you
  are out at the open, worse than you planned.
* **Costs are charged on entry and exit** — spread, slippage and commission.
* **One position per symbol at a time.** Overlapping the same setup twenty bars
  running would multiply one idea into twenty "independent" wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .instruments import get_instrument, pips_between, price_from_pips
from .models import Candle, Direction, Outcome, Signal, Trade
from .risk import total_cost_pips
from .precompute import build_cache
from .scanner import Evaluation, evaluate_at
from .strategy import get_strategy


@dataclass
class BacktestResult:
    """Trades plus the bookkeeping needed to describe how they were produced."""

    symbol: str
    trades: list[Trade] = field(default_factory=list)
    bars_tested: int = 0
    signals_generated: int = 0
    signals_skipped_in_trade: int = 0
    first_bar: str = ""
    last_bar: str = ""
    label: str = "all"
    # One evaluation per trade, in the same order, when the caller asked for
    # them. The ledger rebuilds "what the model saw" from these; nothing in the
    # metrics reads them.
    evaluations: list[Evaluation] = field(default_factory=list)

    @property
    def resolved(self) -> list[Trade]:
        return [t for t in self.trades if t.is_resolved]


def _hit_long(candle: Candle, stop: float, target: float) -> Outcome:
    """Resolve a long position against one bar, resolving ties against us."""
    if candle.low <= stop:
        return Outcome.LOSS  # checked first: a tie inside the bar is a loss
    if candle.high >= target:
        return Outcome.WIN
    return Outcome.OPEN


def _hit_short(candle: Candle, stop: float, target: float) -> Outcome:
    """Resolve a short position against one bar, resolving ties against us."""
    if candle.high >= stop:
        return Outcome.LOSS
    if candle.low <= target:
        return Outcome.WIN
    return Outcome.OPEN


def _excursions(
    candle: Candle, signal: Signal, entry: float, risk_price: float
) -> tuple[float, float]:
    """Adverse and favourable excursion of one bar, in R.

    Tracking these tells the user whether their stops are barely surviving (MAE
    near -1R on winners) or their targets are barely reached — information a win
    rate alone hides.
    """
    if risk_price <= 0:
        return 0.0, 0.0
    if signal.direction is Direction.LONG:
        return (candle.low - entry) / risk_price, (candle.high - entry) / risk_price
    return (entry - candle.high) / risk_price, (entry - candle.low) / risk_price


def simulate_trade(
    candles: list[Candle],
    signal: Signal,
    signal_index: int,
    config: Config,
    instrument,
) -> Trade | None:
    """Carry one signal forward until it resolves, or return None if unfillable."""
    entry_index = signal_index + 1
    if entry_index >= len(candles):
        return None  # signal on the final bar: nothing to fill against

    cost_pips = total_cost_pips(instrument, config)
    slip = price_from_pips(instrument, cost_pips)
    sign = signal.direction.sign

    # Fill at the next open, moved against us by the full spread+slippage.
    fill = candles[entry_index].open + sign * slip
    stop, target = signal.stop_loss, signal.take_profit

    # Costs push the fill toward the stop, so the realised risk is a little
    # larger than the planned risk. Measure R against what actually happened.
    risk_price = (fill - stop) * sign
    if risk_price <= 0:
        # The gap on the open already ran past the stop: a full loss at open.
        return Trade(
            signal=signal,
            entry_time=candles[entry_index].timestamp,
            exit_time=candles[entry_index].timestamp,
            exit_price=candles[entry_index].open,
            outcome=Outcome.LOSS,
            r_multiple=-1.0,
            bars_held=0,
            fill_price=fill,
        )

    resolver = _hit_long if signal.direction is Direction.LONG else _hit_short
    mae, mfe = 0.0, 0.0
    last = min(entry_index + config.backtest.max_bars_in_trade, len(candles) - 1)

    for i in range(entry_index, last + 1):
        candle = candles[i]
        bar_mae, bar_mfe = _excursions(candle, signal, fill, risk_price)
        mae, mfe = min(mae, bar_mae), max(mfe, bar_mfe)

        # A gap through a level fills at the open, which is worse than the level.
        gapped_stop = (candle.open - stop) * sign <= 0
        gapped_target = (candle.open - target) * sign >= 0

        outcome = resolver(candle, stop, target)
        if outcome is Outcome.OPEN:
            continue

        if outcome is Outcome.LOSS:
            exit_price = candle.open if gapped_stop else stop
        else:
            exit_price = candle.open if gapped_target else target

        realised = (exit_price - fill) * sign
        return Trade(
            signal=signal,
            entry_time=candles[entry_index].timestamp,
            exit_time=candle.timestamp,
            exit_price=round(exit_price, instrument.digits),
            outcome=outcome,
            r_multiple=realised / risk_price,
            bars_held=i - entry_index,
            mae_r=round(mae, 3),
            mfe_r=round(mfe, 3),
            fill_price=fill,
        )

    # Ran out of patience: close at the last bar's close and mark it expired.
    final = candles[last]
    realised = (final.close - fill) * sign
    return Trade(
        signal=signal,
        entry_time=candles[entry_index].timestamp,
        exit_time=final.timestamp,
        exit_price=round(final.close, instrument.digits),
        outcome=Outcome.EXPIRED,
        r_multiple=realised / risk_price,
        bars_held=last - entry_index,
        mae_r=round(mae, 3),
        mfe_r=round(mfe, 3),
        fill_price=fill,
    )


def run_backtest(
    candles: list[Candle],
    symbol: str,
    config: Config,
    start: int | None = None,
    end: int | None = None,
    label: str = "all",
    strategy=None,
    keep_evaluations: bool = False,
) -> BacktestResult:
    """Walk the series bar by bar, taking one position at a time.

    ``start``/``end`` bound the *decision* bars, letting the caller split a series
    into in-sample and out-of-sample halves without ever letting the two share a
    trade.

    ``keep_evaluations`` stores the evaluation behind each trade on the result,
    so a replay can show every check the model ran, not just the ones that
    fired. Off by default: a context holds the visible candle slice, and a
    thousand of them is memory a calibration sweep does not need.
    """
    strategy = strategy or get_strategy(config.strategy.name)
    instrument = get_instrument(symbol)

    warmup = max(config.strategy.ema_trend, config.strategy.adx_period * 3) + 5
    first = warmup if start is None else max(start, warmup)
    last = len(candles) - 2 if end is None else min(end, len(candles) - 2)

    result = BacktestResult(symbol=symbol.upper(), label=label)
    if first > last:
        return result

    result.first_bar = candles[first].timestamp.isoformat()
    result.last_bar = candles[last].timestamp.isoformat()

    # Built once for the whole series; see precompute for why this is causal.
    cache = build_cache(candles, config)

    busy_until = -1  # index through which a position is already open
    for i in range(first, last + 1):
        result.bars_tested += 1
        if i <= busy_until:
            continue

        evaluation = evaluate_at(candles, i, symbol, config, strategy, cache)
        if not evaluation.has_signal:
            continue
        result.signals_generated += 1

        trade = simulate_trade(candles, evaluation.signal, i, config, instrument)
        if trade is None:
            continue
        result.trades.append(trade)
        if keep_evaluations:
            result.evaluations.append(evaluation)
        busy_until = i + 1 + trade.bars_held

    return result


def split_backtest(
    candles: list[Candle], symbol: str, config: Config, split: float = 0.7, strategy=None
) -> tuple[BacktestResult, BacktestResult]:
    """Run in-sample and out-of-sample halves.

    Out-of-sample is the only number worth quoting. In-sample is shown beside it
    so the gap between them is visible: a large gap is the signature of a
    strategy tuned to its own history.
    """
    if not 0.1 <= split <= 0.9:
        raise ValueError(f"split must be between 0.1 and 0.9, got {split}")
    boundary = int(len(candles) * split)
    in_sample = run_backtest(candles, symbol, config, end=boundary - 1, label="in-sample",
                             strategy=strategy)
    out_sample = run_backtest(candles, symbol, config, start=boundary, label="out-of-sample",
                              strategy=strategy)
    return in_sample, out_sample
