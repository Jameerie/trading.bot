"""The handholding: what to do, in what order, and what to do when it goes wrong.

A signal card that prints four prices and a lot size assumes the reader already
knows how to trade. This module assumes they do not, and writes down the parts
that usually live in someone's head:

* how to place the order, in the words a broker platform actually uses;
* when to be at the screen for *this* pair, in the reader's own timezone;
* what to do while the trade is running — which, for a system measured on
  set-and-forget rules, is nothing, and it says so and explains why;
* what invalidates the idea before it triggers;
* what to do about a gap, a missed entry, or news;
* and when there is no setup, precisely which conditions failed and what would
  have to change — which is the part that teaches, and the part a bare
  "no setup on H1" throws away.

Nothing here changes a decision. It renders one.
"""

from __future__ import annotations

import textwrap
from datetime import datetime

from .clock import Clock, humanise_delta, next_session_open, session_windows
from .config import Config
from .instruments import Instrument, currency_name
from .models import Direction, Signal, Timeframe, utc_now
from .sessions import is_weekend, session_label
from .strategy.confluence import ConfluenceResult

# What each confluence check is looking for, and what would make it fire. The
# second half is the useful half: "BOS missing" tells a beginner nothing, while
# "price has not yet broken the last swing high" tells them what to watch for.
CHECK_GUIDE: dict[str, tuple[str, str]] = {
    "HTF_ALIGN": (
        "higher-timeframe trend agrees",
        "the H4 structure needs to be making higher highs (for a buy) or lower lows "
        "(for a sell). Right now it is not, so any trade here is against the bigger move",
    ),
    "BOS": (
        "a recent break of structure in our direction",
        "price needs to close beyond the last swing high (buy) or swing low (sell). "
        "Until it does, the trend is not being paid for and the target is a guess",
    ),
    "STRUCTURE": (
        "swing sequence on this timeframe agrees",
        "the chart needs a clean run of higher highs and higher lows (buy), or lower "
        "lows and lower highs (sell). A choppy sequence means no trend to follow",
    ),
    "EMA_STACK": (
        "moving averages stacked in order",
        "the 21 EMA needs to sit above the 50, and the 50 above the 200, for a buy — "
        "reversed for a sell. Tangled averages mean the market has not decided",
    ),
    "PULLBACK": (
        "price has retraced into a zone worth entering",
        "price needs to come back into the EMA band, an unfilled gap, or an order "
        "block. Entering away from one of those means a wide stop and no 1:4",
    ),
    "ADX": (
        "trend strength above the threshold",
        "ADX needs to rise above the configured minimum. Below it, the market is "
        "ranging, and a distant target in a range does not get reached",
    ),
    "EMA_SLOPE": (
        "the fast average is actually moving",
        "the 21 EMA needs to be rising (buy) or falling (sell), not flat. A flat "
        "average on the right side of price is a trend that has already stopped",
    ),
    "SWEEP": (
        "a recent liquidity sweep, rejected",
        "price needs to spike through a recent low (buy) or high (sell) and close "
        "back inside. That spike is where the stops sat, and it often ends the pullback",
    ),
    "DI": (
        "directional index favours our side",
        "+DI needs to be above -DI for a buy, and below it for a sell. When they are "
        "crossed the other way, the pressure is against the trade",
    ),
    "RSI_ROOM": (
        "momentum has room left to run",
        "RSI needs to be in the 40-70 band for a buy, or 30-60 for a sell. Outside "
        "that, most of the move to the target has already happened",
    ),
    "SESSION": (
        "we are inside a liquid session",
        "the bar needs to close inside a session window. Outside them the spread is "
        "wider and the volume that carries price to a distant target is not there",
    ),
    "MOMENTUM": (
        "the decision bar closed with conviction",
        "the last closed candle needs a decisive body in our direction, or a "
        "rejection wick against us. A doji says the market is undecided",
    ),
}


def _fmt(price: float, digits: int) -> str:
    return f"{price:.{digits}f}"


def wrap(text: str, indent: str = "        ", first: str | None = None, width: int = 78) -> list[str]:
    """Wrap a sentence to the card width, keeping hanging indents readable.

    Advice that runs off the right edge of a terminal is advice nobody reads, and
    this module writes in sentences rather than in columns.
    """
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=first if first is not None else indent,
        subsequent_indent=indent,
    ) or [first or indent]


def order_ticket(signal: Signal, instrument: Instrument, config: Config) -> list[str]:
    """The order, written the way a broker platform asks for it.

    A pending order rather than a market order, because the measured results
    assume a fill at the next bar's open near this price. Chasing the price after
    it has run is a different trade with a worse ratio, and it is the single most
    common way a good setup becomes a bad one.
    """
    d = instrument.digits
    side = "BUY" if signal.direction is Direction.LONG else "SELL"
    order_type = "Buy Limit" if signal.direction is Direction.LONG else "Sell Limit"
    money = f"{signal.risk_amount:,.2f} {signal.account_currency}"
    reward = f"{signal.risk_amount * signal.risk_reward:,.2f} {signal.account_currency}"

    return [
        "  HOW TO PLACE IT  (type these into your broker, in this order)",
        f"    1. Symbol       {signal.symbol}  ({instrument.describe()})",
        f"    2. Order type   {order_type}   — not a market order",
        f"    3. Price        {_fmt(signal.entry, d)}",
        f"    4. Volume       {signal.position_lots:.2f} lots"
        f"   ({signal.position_units:,.0f} units)",
        f"    5. Stop loss    {_fmt(signal.stop_loss, d)}"
        f"   ({signal.risk_pips:.1f} pips away)",
        f"    6. Take profit  {_fmt(signal.take_profit, d)}"
        f"   ({signal.reward_pips:.1f} pips away)",
        "",
        f"    Before you hit confirm, check the ticket says {signal.position_lots:.2f} lots and "
        f"not {signal.position_lots * 100:.0f}.",
        f"    A decimal in the wrong place is the most expensive typo in this business.",
        f"    Placed as written, this risks {money} to make {reward}.",
        f"    If your platform will not accept {signal.position_lots:.2f}, round DOWN, never up.",
    ]


def timing_plan(
    signal: Signal, instrument: Instrument, clock: Clock, now: datetime | None = None
) -> list[str]:
    """When to be at the screen for this pair, in the reader's own clock."""
    now = now or utc_now()
    age = now - signal.issued_at
    lines = [
        "  WHEN TO WATCH IT",
        f"    Signalled at  {clock.stamp(signal.issued_at)}"
        f"   ({humanise_delta(signal.issued_at - now)})",
        f"    Session then  {session_label(signal.issued_at)}",
    ]

    # A card built from stale candles is a history lesson, not a trade. Say so
    # before anything else on this block, because everything below assumes the
    # price is still near the entry.
    stale_bars = age.total_seconds() / 60 / max(signal.timeframe.minutes, 1)
    if stale_bars > 4:
        lines += wrap(
            f"NOT ACTIONABLE: this bar closed {humanise_delta(signal.issued_at - now)}, "
            f"about {stale_bars:.0f} bars back. The entry has long since been passed or "
            f"invalidated. Re-scan on fresh data before placing anything.",
            indent="    ",
            first="    ! ",
        )

    windows = session_windows(clock, list(instrument.peak_sessions))
    if windows:
        joined = "; ".join(f"{w.name} {w.label}" for w in windows)
        lines.append(f"    {signal.symbol} is liquid in {joined} {clock.abbrev(now)}")
        lines.append(
            f"    Outside those hours the spread widens and a {signal.risk_reward:.1f}R target "
            f"stops being reachable."
        )

    upcoming = next_session_open(now, list(instrument.peak_sessions))
    if upcoming is not None:
        name, when = upcoming
        lines.append(
            f"    Next {name} open: {clock.stamp(when)} ({humanise_delta(when - now)})"
        )
    if is_weekend(now):
        lines.append(
            "    The market is shut right now. Nothing fills until Sunday night, and the "
            "Monday open can gap straight past this entry."
        )
    return lines


def management_plan(signal: Signal, config: Config, clock: Clock) -> list[str]:
    """What to do once the order is live.

    The honest answer is "nothing", and the reason matters: every measured number
    this tool reports was produced by a simulation that placed the stop and the
    target and then left them alone. Move the stop to breakeven at +1R and you
    may well do better — but you will be trading a system nobody has measured,
    and the win rate on the card no longer describes it.
    """
    timeframe = Timeframe.parse(config.data.timeframe)
    hours = config.backtest.max_bars_in_trade * timeframe.minutes / 60
    return [
        "  WHILE IT IS RUNNING",
        "    Do nothing. Leave the stop and the target where they are.",
        "",
        "    That is not laziness — it is the only version of this trade that has been",
        f"    measured. Every win rate on this card came from a simulation that set the",
        f"    two levels and did not touch them for up to {config.backtest.max_bars_in_trade} bars "
        f"(about {hours:.0f} market hours).",
        "    Move the stop to breakeven, take half off, or trail it, and you may do better —",
        "    but you are then trading a system with no measured record, and the numbers",
        "    here stop describing it.",
        "",
        "    Two things are always wrong:",
        "      - Widening the stop. It converts a planned 1% loss into an unplanned one.",
        "      - Adding to a loser. That is a second trade, taken because the first hurts.",
    ]


def invalidation_plan(signal: Signal, instrument: Instrument, config: Config) -> list[str]:
    """What would make this wrong before it ever triggers."""
    d = instrument.digits
    long = signal.direction is Direction.LONG
    beyond = "below" if long else "above"
    return [
        "  WHAT WOULD MAKE THIS WRONG",
        f"    Before it fills: if price closes {beyond} {_fmt(signal.stop_loss, d)} without "
        f"touching {_fmt(signal.entry, d)},",
        f"    the structure this trade rests on is gone. Cancel the order — do not chase it.",
        f"    After it fills: the stop at {_fmt(signal.stop_loss, d)} is the answer. It sits "
        f"beyond the swing",
        "    that would prove the idea wrong, plus a buffer, so that an ordinary wick does",
        "    not take you out but a genuine break does.",
        "",
        "    If it stops out, the trade was not a mistake. At this ratio most of them lose,",
        f"    and the {signal.risk_reward:.1f}R on the winners is where the money is. A stop-out",
        "    only means something went wrong if you did not follow the plan.",
    ]


def contingencies(signal: Signal, instrument: Instrument, config: Config) -> list[str]:
    """The what-ifs, answered before they happen."""
    d = instrument.digits
    return [
        "  WHAT IF...",
        f"    ...price is already past {_fmt(signal.entry, d)} when you look?",
        "       Skip it. The entry is what makes the ratio; without it this is a worse trade.",
        f"    ...the order does not fill within {config.backtest.entry_expiry_bars} bars?",
        "       Cancel it. The setup is stale and the measurement assumed a prompt fill.",
        "    ...it gaps over the stop at the open?",
        "       You are out at the open price, which is worse than the stop. That is already",
        "       counted in the numbers on this card, and it is why the risk figure is a",
        "       floor rather than a guarantee.",
        "    ...there is news due on one of these currencies?",
        f"       {currency_name(instrument.base)} or {currency_name(instrument.quote)} data can move price",
        "       straight through both levels. This tool does not read a news calendar —",
        "       check one yourself, and stand aside if something big is due.",
        "    ...you are unsure?",
        "       Not taking it is a position. The next setup costs nothing to wait for.",
    ]


def aftercare(signal: Signal, config: Config) -> list[str]:
    """Closing the loop — the part that turns advice into a measurable record."""
    entry_id = f"{signal.symbol}@{signal.issued_at.isoformat()}"
    return [
        "  AFTERWARDS  (this is what makes the next prediction better)",
        "    Whatever happens, record it:",
        f"      python -m trading_bot journal --close \"{entry_id}\" --exit <your exit price>",
        "    Or let the tool settle it against real candles for you:",
        "      python -m trading_bot forecast --resolve",
        "",
        "    An unrecorded trade is invisible to the loss limits, to the win rate, and to",
        "    every base rate this tool will quote you next week. The scoreboard only knows",
        "    what you tell it.",
    ]


def explain_no_signal(
    symbol: str,
    timeframe: str,
    fraction: float | None,
    confluence: ConfluenceResult | None,
    direction: Direction | None,
    config: Config,
    instrument: Instrument | None = None,
    clock: Clock | None = None,
) -> list[str]:
    """Say precisely why there is no trade here, and what would change that.

    This is the output the user sees most often — a selective system says no far
    more than it says yes — so it is worth more than one line. A near miss is a
    watchlist item; a market with nothing going on is not, and the difference is
    visible here.
    """
    threshold = config.strategy.min_confluence
    header = f"  {symbol}  -  no trade on {timeframe}"
    if fraction is None or confluence is None or direction is None:
        return [
            header,
            "    Nothing to score: the market has no directional bias here, or the",
            "    indicators are still warming up on the history available.",
        ]

    gap = threshold - fraction
    side = "buy" if direction is Direction.LONG else "sell"
    lines = [
        header,
        f"    Best case is a {side}, scoring {confluence.score:.0f} of "
        f"{confluence.max_score:.0f} points ({fraction:.0%}).",
        f"    It needs {threshold:.0%} to become a signal, so it is "
        f"{gap * 100:.0f} points of confluence short.",
    ]

    if confluence.reasons:
        lines.append("")
        lines.append("    Already true:")
        for reason in confluence.reasons:
            lines.append(f"      + {reason.detail}")

    missing = [code for code in confluence.missing]
    if missing:
        lines.append("")
        lines.append("    Still needed — this is what to watch for:")
        weights = {c: w for c, w in _weights(confluence)}
        for code in sorted(missing, key=lambda c: weights.get(c, 0), reverse=True)[:5]:
            title, detail = CHECK_GUIDE.get(code, (code, "no description available"))
            lines += wrap(
                f"{title} (+{weights.get(code, 0):.0f}): {detail}",
                indent=" " * 8,
                first="      - ",
            )

    if fraction >= threshold - 0.12:
        lines.append("")
        lines += wrap(
            f"This is close. Put {symbol} on your watchlist and re-scan at the next "
            f"bar close.",
            indent="    ",
        )
    elif fraction < 0.4:
        lines.append("")
        lines += wrap(
            f"This is not close. There is no trend here to follow — leave {symbol} "
            f"alone rather than watching it.",
            indent="    ",
        )

    if instrument is not None and clock is not None:
        upcoming = next_session_open(utc_now(), list(instrument.peak_sessions))
        if upcoming is not None:
            name, when = upcoming
            lines += wrap(
                f"Next chance for this pair to move: {name} open, {clock.stamp(when)}.",
                indent="    ",
            )
    return lines


def _weights(confluence: ConfluenceResult) -> list[tuple[str, float]]:
    """Recover each check's weight, including the ones that did not fire.

    Fired checks carry their own weight; missing ones do not, so their weight is
    read from the strategy's default table. Falling back to the table rather than
    to zero keeps the "still needed" list ordered by what actually matters.
    """
    from .strategy.trend_pullback import DEFAULT_CHECKS

    table = {check.code: check.weight for check in DEFAULT_CHECKS}
    for reason in confluence.reasons:
        table[reason.code] = reason.weight
    return list(table.items())


def daily_briefing(config: Config, clock: Clock, now: datetime | None = None) -> list[str]:
    """The top-of-scan orientation: what time it is, and what that means today."""
    now = now or utc_now()
    lines = [
        f"  {clock.day(now)} - {clock.short(now)} "
        f"({now.strftime('%H:%M')} UTC)",
    ]
    if is_weekend(now):
        lines.append(
            "  The FX market is closed. Anything below is a plan for the next open, not"
        )
        lines.append("  something to place now.")
        return lines

    label = session_label(now)
    lines.append(f"  Session now: {label}")

    windows = session_windows(clock, config.strategy.sessions)
    if windows:
        joined = ";  ".join(f"{w.name} {w.label}" for w in windows)
        lines.append(f"  Your trading windows ({clock.abbrev(now)}): {joined}")
    if label in ("off-session", "market closed"):
        upcoming = next_session_open(now, config.strategy.sessions)
        if upcoming is not None:
            name, when = upcoming
            lines.append(
                f"  Nothing is liquid right now. {name} opens {clock.stamp(when)} "
                f"({humanise_delta(when - now)})."
            )
    return lines
