"""Signal construction and rendering.

This module is the product. Everything upstream exists so that the card printed
by ``format_signal`` is worth reading: what to trade, which way, where to get in,
where to get out if wrong, where to take profit, how much to risk, and why.

``build_signal`` is the only sanctioned way to create a Signal, because it is
where the risk-to-reward floor is enforced.
"""

from __future__ import annotations

from .clock import Clock
from .config import Config
from .errors import RiskError
from .instruments import Instrument, currency_name, pips_between
from .models import Direction, Reason, Signal, Timeframe
from .risk import build_stop_target, enforce_rr, position_size, structural_stop
from .sessions import session_label
from .strategy.base import MarketContext
from .strategy.confluence import ConfluenceResult


def build_signal(
    ctx: MarketContext,
    direction: Direction,
    confluence: ConfluenceResult,
    stop_reference: float,
    target_reference: float | None,
    strategy_name: str,
) -> Signal | None:
    """Turn a scored setup into a Signal, or return None if it fails the rules.

    Returning None rather than raising is deliberate: a setup that cannot pay 1:4
    is an ordinary, expected outcome during a scan, not an error condition. The
    only thing that reaches the user is a setup that passed every rule.
    """
    instrument = ctx.instrument
    config = ctx.config
    entry = ctx.price
    atr_value = ctx.atr or 0.0

    stop = structural_stop(direction, entry, stop_reference, atr_value, instrument, config)

    try:
        setup = build_stop_target(
            direction=direction,
            entry=entry,
            stop_loss=stop,
            instrument=instrument,
            config=config,
            target_level=target_reference,
        )
        enforce_rr(setup, config)
        size = position_size(instrument, setup.entry, setup.stop_loss, config)
    except RiskError:
        # The setup looked good but the numbers do not pay. Discard it silently;
        # this is the floor doing its job, not a failure worth surfacing.
        return None

    if size.lots <= 0:
        return None

    warnings = _collect_warnings(ctx, setup.risk_pips, size.approximate, target_reference)

    return Signal(
        symbol=ctx.symbol,
        timeframe=Timeframe.parse(config.data.timeframe),
        direction=direction,
        entry=setup.entry,
        stop_loss=setup.stop_loss,
        take_profit=setup.take_profit,
        issued_at=ctx.timestamp,
        score=confluence.score,
        max_score=confluence.max_score,
        risk_reward=setup.risk_reward,
        risk_pips=setup.risk_pips,
        reward_pips=setup.reward_pips,
        position_units=size.units,
        position_lots=size.lots,
        risk_amount=size.risk_amount,
        account_currency=config.account.currency,
        reasons=confluence.reasons,
        warnings=tuple(warnings),
        strategy=strategy_name,
    )


def _collect_warnings(
    ctx: MarketContext, risk_pips: float, approximate_pip_value: bool, target_reference: float | None
) -> list[str]:
    """Flag anything the human should weigh before taking the trade.

    Warnings never block a signal — they inform it. The user asked to see what to
    do, which includes seeing what is uncertain about it.
    """
    warnings: list[str] = []
    config = ctx.config

    if approximate_pip_value:
        warnings.append(
            f"pip value for {ctx.symbol} in {config.account.currency} is approximate; "
            "confirm the lot size with your broker before placing this"
        )
    if risk_pips <= config.risk.min_stop_pips + 0.5:
        warnings.append(
            f"stop is at the {config.risk.min_stop_pips:.0f}-pip minimum, so it sits close "
            "to price and is more exposed to noise"
        )
    if target_reference is None:
        warnings.append(
            "no structural target above/below price; the take profit is placed at the "
            "minimum-R:R distance rather than at a level the market is reaching for"
        )
    if ctx.adx is not None and ctx.adx < config.strategy.adx_min + 3:
        warnings.append(f"ADX {ctx.adx:.1f} is only just above the trend threshold")
    label = session_label(ctx.timestamp)
    if label in ("off-session", "market closed"):
        warnings.append(f"signal formed {label}; liquidity may not support the target")
    return warnings


def _fmt(price: float, digits: int) -> str:
    return f"{price:.{digits}f}"


def plain_english(signal: Signal, instrument: Instrument) -> list[str]:
    """The trade in sentences, before any of it is in jargon.

    Someone who has never placed a forex trade should be able to read this block
    alone and know what is being suggested and what it costs to be wrong.
    """
    d = instrument.digits
    verb = "Buy" if signal.direction is Direction.LONG else "Sell"
    base = currency_name(instrument.base)
    quote = currency_name(instrument.quote)
    expect = "rise" if signal.direction is Direction.LONG else "fall"
    reward = signal.risk_amount * signal.risk_reward

    return [
        "  IN ONE LINE",
        f"    {verb} the {base} against the {quote} at around {_fmt(signal.entry, d)},",
        f"    because it looks likely to {expect} to {_fmt(signal.take_profit, d)}.",
        f"    You are risking {signal.risk_amount:,.2f} {signal.account_currency} to make "
        f"{reward:,.2f} {signal.account_currency}.",
        f"    If it reaches {_fmt(signal.stop_loss, d)} instead, you lose the "
        f"{signal.risk_amount:,.2f} and the trade is over.",
        f"    That is the entire downside — there is no way to lose more than that on this",
        f"    trade, provided you place the stop loss at the same time as the entry.",
    ]


def format_signal(
    signal: Signal,
    instrument: Instrument,
    config: Config | None = None,
    clock: Clock | None = None,
    prediction=None,
    width: int = 78,
    detail: str = "full",
) -> str:
    """Render the human-readable 'what to do' card.

    Deliberately plain text: this output gets pasted into notes, journals and
    chat, and it should survive all of them.

    ``detail="full"`` is the default because the product is advice, not a price
    quote — the reader is told how to place it, when to watch it, what would
    make it wrong, and what to do afterwards. ``detail="brief"`` drops the
    coaching for someone who has read it before.
    """
    from .playbook import (
        aftercare,
        contingencies,
        invalidation_plan,
        management_plan,
        order_ticket,
        timing_plan,
    )

    config = config or Config()
    clock = clock or config.clock
    d = instrument.digits
    arrow = "BUY " if signal.direction is Direction.LONG else "SELL"
    bar = "=" * width
    dash = "-" * width
    reward = signal.risk_amount * signal.risk_reward

    lines = [
        bar,
        f"  {arrow} {signal.symbol}   [{signal.grade}]  confidence {signal.confidence:.0%}"
        f"   {signal.risk_reward:.1f}R",
        f"  {instrument.describe()}",
        f"  {signal.timeframe.name} - {clock.stamp(signal.issued_at)}"
        f" - {session_label(signal.issued_at)}",
        bar,
        "",
    ]

    if detail == "full":
        lines += plain_english(signal, instrument)
        lines.append("")

    lines += [
        "  WHAT TO DO",
        f"    Entry        {_fmt(signal.entry, d)}   (at or near this price)",
        f"    Stop loss    {_fmt(signal.stop_loss, d)}   ({signal.risk_pips:.1f} pips risk)",
        f"    Take profit  {_fmt(signal.take_profit, d)}   ({signal.reward_pips:.1f} pips reward)",
        f"    Size         {signal.position_lots:.2f} lots ({signal.position_units:,.0f} units)",
        f"    Risking      {signal.risk_amount:,.2f} {signal.account_currency}"
        f"  to make  {reward:,.2f} {signal.account_currency}",
        "",
    ]

    if prediction is not None:
        from .forecast import format_prediction

        lines += format_prediction(prediction, clock)
        lines.append("")

    if detail == "full":
        lines += order_ticket(signal, instrument, config)
        lines.append("")
        lines += timing_plan(signal, instrument, clock)
        lines.append("")

    lines += [
        dash,
        f"  WHY  ({signal.score:.0f} of {signal.max_score:.0f} confluence points)",
    ]
    for reason in signal.reasons:
        lines.append(f"    + {reason.detail} (+{reason.weight:.0f})")

    if signal.warnings:
        from .playbook import wrap

        lines.append("")
        lines.append("  CHECK BEFORE YOU TAKE IT")
        for warning in signal.warnings:
            lines += wrap(warning, indent="      ", first="    ! ")

    if detail == "full":
        for block in (
            invalidation_plan(signal, instrument, config),
            management_plan(signal, config, clock),
            contingencies(signal, instrument, config),
            aftercare(signal, config),
        ):
            lines.append("")
            lines += block

    lines += [
        "",
        dash,
        "  You place this trade. This tool does not and will not.",
        bar,
    ]
    return "\n".join(lines)


def format_signal_compact(signal: Signal, instrument: Instrument) -> str:
    """One-line form, for scanning many symbols at once."""
    d = instrument.digits
    side = "BUY" if signal.direction is Direction.LONG else "SELL"
    return (
        f"{signal.symbol:<8} {side:<4} {signal.grade:<2} "
        f"entry {_fmt(signal.entry, d):<10} sl {_fmt(signal.stop_loss, d):<10} "
        f"tp {_fmt(signal.take_profit, d):<10} "
        f"{signal.risk_reward:.1f}R  {signal.position_lots:.2f} lots"
    )


def no_signal_message(symbol: str, timeframe: str, best_fraction: float | None = None) -> str:
    """What to print when nothing qualifies, in one line.

    'No setup' is a real answer and should read like one. A tool that never says
    no is a tool that is guessing. The long form — which checks passed, which
    failed, and what would have to change — lives in ``playbook.explain_no_signal``.
    """
    base = f"{symbol:<8} no setup on {timeframe}"
    if best_fraction is not None:
        base += f" (best confluence {best_fraction:.0%})"
    return base
