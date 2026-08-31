"""Trend-pullback strategy — the default engine.

The idea it trades, in one paragraph: find a market with an established
directional bias, wait for structure to break in that direction (confirming
that the bias is still being paid for), then enter on a pullback into the zone
the impulse left behind. Stop goes beyond the structure that would invalidate
the idea; target goes to the next liquidity pool, which in a trending market is
usually far enough away to pay 1:4 or better.

Why this shape suits the goal: entering on a pullback rather than a breakout is
what makes a small stop and a distant target coexist. Chasing a breakout gives
you a wide stop and a near target — the exact inverse of a 1:4 trade.
"""

from __future__ import annotations

from ..models import Direction
from ..sessions import in_any_session, is_weekend
from ..structure import Trend
from .base import MarketContext
from .confluence import Check, ConfluenceEngine


def _trend_for(direction: Direction) -> Trend:
    return Trend.UP if direction is Direction.LONG else Trend.DOWN


def _check_htf_align(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """Higher-timeframe trend agrees. The heaviest weight: never fight the HTF."""
    if ctx.htf_trend is _trend_for(d):
        return True, f"{ctx.config.data.htf_timeframe} structure is trending {ctx.htf_trend.value}"
    return False, ""


def _check_structure_trend(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """Swing sequence on the trading timeframe agrees."""
    if ctx.view.trend is _trend_for(d):
        label = "higher highs and higher lows" if d is Direction.LONG else "lower lows and lower highs"
        return True, f"market structure shows {label}"
    return False, ""


def _check_ema_stack(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """Moving averages stacked in order — a clean, uncontested trend."""
    if None in (ctx.ema_fast, ctx.ema_slow, ctx.ema_trend):
        return False, ""
    s = ctx.config.strategy
    if d is Direction.LONG:
        ok = ctx.ema_fast > ctx.ema_slow > ctx.ema_trend
    else:
        ok = ctx.ema_fast < ctx.ema_slow < ctx.ema_trend
    order = ">" if d is Direction.LONG else "<"
    return ok, f"EMA {s.ema_fast} {order} EMA {s.ema_slow} {order} EMA {s.ema_trend}"


def _check_ema_slope(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """The fast EMA is actually moving, not just sitting on the right side."""
    if ctx.ema_fast_slope is None:
        return False, ""
    ok = ctx.ema_fast_slope > 0 if d is Direction.LONG else ctx.ema_fast_slope < 0
    return ok, f"EMA {ctx.config.strategy.ema_fast} slope is {'rising' if d is Direction.LONG else 'falling'}"


def _check_break_of_structure(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """A recent BOS/CHoCH in our direction — the trend is being paid for now."""
    recent = [b for b in ctx.view.breaks if ctx.index - b.index <= ctx.config.strategy.max_pullback_bars]
    for brk in reversed(recent):
        if brk.direction is d:
            bars = ctx.index - brk.index
            return True, f"{brk.kind} {d.value} at {brk.level:.5f}, {bars} bar(s) ago"
    return False, ""


def _check_pullback_zone(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """Price has retraced into a zone worth buying/selling.

    Three acceptable zones, in order of preference: an unfilled fair value gap,
    an order block, or simply the fast/slow EMA band. Entering *into* one of
    these is what keeps the stop tight.
    """
    price = ctx.price
    for gap in reversed(ctx.view.gaps[-8:]):
        if gap.direction is d and gap.contains(price):
            return True, f"price is inside a {d.value} fair value gap ({gap.bottom:.5f}-{gap.top:.5f})"
    for block in reversed(ctx.view.order_blocks[-6:]):
        if block.direction is d and block.contains(price):
            return True, f"price is inside a {d.value} order block ({block.bottom:.5f}-{block.top:.5f})"
    if ctx.ema_fast is not None and ctx.ema_slow is not None:
        top, bottom = max(ctx.ema_fast, ctx.ema_slow), min(ctx.ema_fast, ctx.ema_slow)
        # Allow a little tolerance either side of the band; a pullback rarely
        # stops exactly on a moving average.
        pad = (ctx.atr or 0.0) * 0.3
        if bottom - pad <= price <= top + pad:
            return True, "price pulled back into the EMA band"
    return False, ""


def _check_adx(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """Trend strength is above the threshold — filters out ranges."""
    if ctx.adx is None:
        return False, ""
    threshold = ctx.config.strategy.adx_min
    return ctx.adx >= threshold, f"ADX {ctx.adx:.1f} is above the {threshold:.0f} trend threshold"


def _check_di_alignment(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """Directional index agrees with the side we want to take."""
    if ctx.plus_di is None or ctx.minus_di is None:
        return False, ""
    ok = ctx.plus_di > ctx.minus_di if d is Direction.LONG else ctx.minus_di > ctx.plus_di
    return ok, f"+DI {ctx.plus_di:.1f} vs -DI {ctx.minus_di:.1f} favours {d.value}"


def _check_rsi_room(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """RSI is not already exhausted.

    Buying at RSI 82 means most of the move to the target has already happened,
    which is precisely when a 4R target stops being reachable.
    """
    if ctx.rsi is None:
        return False, ""
    ok = 40.0 <= ctx.rsi <= 70.0 if d is Direction.LONG else 30.0 <= ctx.rsi <= 60.0
    return ok, f"RSI {ctx.rsi:.1f} leaves room to run"


def _check_liquidity_sweep(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """A recent stop run against us, rejected — often the low of the pullback."""
    for sweep in reversed(ctx.view.sweeps[-5:]):
        if sweep.direction is d and ctx.index - sweep.index <= 5:
            return True, f"liquidity swept at {sweep.swept_level:.5f} and rejected"
    return False, ""


def _check_session(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """We are inside a liquid session."""
    s = ctx.config.strategy
    if s.avoid_weekend and is_weekend(ctx.timestamp):
        return False, ""
    if in_any_session(ctx.timestamp, s.sessions):
        return True, f"inside the {'/'.join(s.sessions) or 'any'} session window"
    return False, ""


def _check_momentum_bar(ctx: MarketContext, d: Direction) -> tuple[bool, str]:
    """The decision bar itself closed in our direction with conviction."""
    candle = ctx.candle
    right_way = candle.is_bullish if d is Direction.LONG else candle.is_bearish
    decisive = candle.body_ratio >= 0.5
    rejection = (
        candle.lower_wick > candle.body if d is Direction.LONG else candle.upper_wick > candle.body
    )
    if right_way and decisive:
        return True, f"decisive {d.value} close ({candle.body_ratio:.0%} body)"
    if rejection:
        side = "lower" if d is Direction.LONG else "upper"
        return True, f"{side} wick rejection on the entry bar"
    return False, ""


# Weights encode conviction. Higher-timeframe alignment and a fresh break of
# structure carry the most because they are what make a distant target realistic;
# oscillator readings carry the least because they are the easiest to overfit.
DEFAULT_CHECKS = [
    Check("HTF_ALIGN", 20.0, _check_htf_align),
    Check("BOS", 15.0, _check_break_of_structure),
    Check("STRUCTURE", 15.0, _check_structure_trend),
    Check("EMA_STACK", 12.0, _check_ema_stack),
    Check("PULLBACK", 12.0, _check_pullback_zone),
    Check("ADX", 8.0, _check_adx),
    Check("EMA_SLOPE", 8.0, _check_ema_slope),
    Check("SWEEP", 8.0, _check_liquidity_sweep),
    Check("DI", 6.0, _check_di_alignment),
    Check("RSI_ROOM", 6.0, _check_rsi_room),
    Check("SESSION", 6.0, _check_session),
    Check("MOMENTUM", 6.0, _check_momentum_bar),
]


class TrendPullbackStrategy:
    """Confluence-scored trend continuation entries on a pullback."""

    name = "trend_pullback"

    def __init__(self, checks: list[Check] | None = None) -> None:
        self.engine = ConfluenceEngine(checks or list(DEFAULT_CHECKS))

    def candidate_direction(self, ctx: MarketContext) -> Direction | None:
        """Pick the side to test, from the weightiest evidence available.

        Only one direction is ever scored. Scoring both and taking the better one
        invites a signal in a market that is simply undecided.
        """
        if ctx.htf_trend is Trend.UP:
            return Direction.LONG
        if ctx.htf_trend is Trend.DOWN:
            return Direction.SHORT
        if ctx.view.trend is Trend.UP:
            return Direction.LONG
        if ctx.view.trend is Trend.DOWN:
            return Direction.SHORT
        if ctx.ema_fast is not None and ctx.ema_slow is not None:
            if ctx.ema_fast > ctx.ema_slow:
                return Direction.LONG
            if ctx.ema_fast < ctx.ema_slow:
                return Direction.SHORT
        return None

    def stop_reference(self, ctx: MarketContext, direction: Direction) -> float | None:
        """The structural level that invalidates the trade if broken."""
        recent = ctx.candles[-ctx.config.strategy.max_pullback_bars :]
        if direction is Direction.LONG:
            swing = ctx.view.last_swing_low
            structural = swing.price if swing else None
            recent_low = min(c.low for c in recent)
            # Take whichever sits closer below price: a stop beyond an old, far
            # swing is a stop we are not really using.
            return max(structural, recent_low) if structural else recent_low
        swing = ctx.view.last_swing_high
        structural = swing.price if swing else None
        recent_high = max(c.high for c in recent)
        return min(structural, recent_high) if structural else recent_high

    def target_reference(self, ctx: MarketContext, direction: Direction) -> float | None:
        """The next liquidity pool the move is likely aiming at."""
        if direction is Direction.LONG:
            highs = [s.price for s in ctx.view.swings if s.is_high and s.price > ctx.price]
            return max(highs) if highs else None
        lows = [s.price for s in ctx.view.swings if s.is_low and s.price < ctx.price]
        return min(lows) if lows else None

    def evaluate(self, ctx: MarketContext):
        """Score the setup and hand a qualifying one to the signal builder.

        Imported here rather than at module scope to keep the import graph
        acyclic: signals depends on strategy types for rendering.
        """
        from ..signals import build_signal

        if not ctx.is_warm:
            return None

        direction = self.candidate_direction(ctx)
        if direction is None:
            return None

        result = self.engine.score(ctx, direction)
        if result.fraction < ctx.config.strategy.min_confluence:
            return None

        stop_ref = self.stop_reference(ctx, direction)
        if stop_ref is None:
            return None

        return build_signal(
            ctx=ctx,
            direction=direction,
            confluence=result,
            stop_reference=stop_ref,
            target_reference=self.target_reference(ctx, direction),
            strategy_name=self.name,
        )
