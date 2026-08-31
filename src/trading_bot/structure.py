"""Market structure: swings, trend state, breaks of structure, and imbalances.

This is where "what is price actually doing" gets decided. Everything is
computed from *closed* candles up to a given index — no function here may look
at ``candles[i + 1]``. The confirmation delay in ``swing_points`` exists for
exactly that reason and is the most important detail in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import Candle, Direction


class Trend(Enum):
    """Structural trend derived from the sequence of swing points."""

    UP = "up"
    DOWN = "down"
    RANGE = "range"


@dataclass(frozen=True)
class Swing:
    """A confirmed pivot high or low."""

    index: int
    price: float
    is_high: bool
    confirmed_at: int  # bar index at which this pivot became knowable

    @property
    def is_low(self) -> bool:
        return not self.is_high


@dataclass(frozen=True)
class StructureBreak:
    """A break of structure (BOS) or change of character (CHoCH).

    BOS = trend continuation: price takes out the prior swing in the trend's
    direction. CHoCH = potential reversal: price takes out the most recent
    counter-trend swing, i.e. the first crack in the existing sequence.
    """

    index: int
    direction: Direction
    level: float
    kind: str  # "BOS" or "CHOCH"


@dataclass(frozen=True)
class FairValueGap:
    """A three-bar imbalance where price moved too fast to trade an area.

    Bullish gap: candle[i-1].high < candle[i+1].low, leaving the zone between
    them untraded. These often act as pullback targets.
    """

    index: int
    direction: Direction
    top: float
    bottom: float

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass(frozen=True)
class OrderBlock:
    """The last opposing candle before an impulsive move that broke structure.

    Treated as a demand zone (bullish) or supply zone (bearish) that price may
    revisit before continuing.

    ``confirmed_at`` is the index of the break that revealed the block, which is
    always later than the block's own candle. Visibility must be filtered on
    ``confirmed_at``: an order block at bar 5 discovered by a break at bar 50 was
    not knowable at bar 10.
    """

    index: int
    direction: Direction
    top: float
    bottom: float
    confirmed_at: int = 0

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass(frozen=True)
class LiquiditySweep:
    """A wick through a prior swing that closes back inside it.

    Interpreted as stops being taken before a move the other way — a rejection,
    not a breakout. Requires the *close* to come back, which is what separates it
    from a genuine break.
    """

    index: int
    direction: Direction  # direction of the expected move after the sweep
    swept_level: float


def swing_points(candles: list[Candle], left: int = 2, right: int = 2) -> list[Swing]:
    """Find fractal pivots with ``left`` bars either side.

    A pivot at index ``i`` is only *confirmed* at index ``i + right``, because
    that is the first bar at which the right-hand side is fully known. Callers
    evaluating bar ``n`` must filter on ``confirmed_at <= n``; ``swings_known_at``
    does this for you. Skipping that check leaks the future into the past.
    """
    if left < 1 or right < 1:
        raise ValueError("left and right must both be >= 1")
    out: list[Swing] = []
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        pivot = candles[i]
        others = [c for j, c in enumerate(window) if j != left]
        if all(pivot.high >= c.high for c in others) and any(
            pivot.high > c.high for c in others
        ):
            out.append(Swing(i, pivot.high, True, i + right))
        if all(pivot.low <= c.low for c in others) and any(pivot.low < c.low for c in others):
            out.append(Swing(i, pivot.low, False, i + right))
    out.sort(key=lambda s: (s.index, s.is_high))
    return out


def swings_known_at(swings: list[Swing], index: int) -> list[Swing]:
    """Filter to the swings a decision made at ``index`` is allowed to see."""
    return [s for s in swings if s.confirmed_at <= index]


def classify_trend(swings: list[Swing], lookback: int = 4) -> Trend:
    """Read trend from the last few highs and lows.

    Higher highs *and* higher lows is an uptrend; the mirror is a downtrend;
    anything mixed is a range. Requiring both sides to agree keeps us out of
    choppy conditions, which is where a high-R:R strategy bleeds.
    """
    highs = [s for s in swings if s.is_high][-lookback:]
    lows = [s for s in swings if s.is_low][-lookback:]
    if len(highs) < 2 or len(lows) < 2:
        return Trend.RANGE
    higher_highs = highs[-1].price > highs[-2].price
    higher_lows = lows[-1].price > lows[-2].price
    lower_highs = highs[-1].price < highs[-2].price
    lower_lows = lows[-1].price < lows[-2].price
    if higher_highs and higher_lows:
        return Trend.UP
    if lower_highs and lower_lows:
        return Trend.DOWN
    return Trend.RANGE


def detect_structure_breaks(
    candles: list[Candle], swings: list[Swing], up_to: int | None = None
) -> list[StructureBreak]:
    """Find BOS/CHoCH events on closes, using only swings confirmed beforehand."""
    end = len(candles) - 1 if up_to is None else up_to
    breaks: list[StructureBreak] = []
    trend = Trend.RANGE
    broken: set[tuple[int, bool]] = set()
    for i in range(end + 1):
        visible = swings_known_at(swings, i)
        if not visible:
            continue
        close = candles[i].close
        highs = [s for s in visible if s.is_high and s.index < i]
        lows = [s for s in visible if s.is_low and s.index < i]
        if highs:
            last_high = highs[-1]
            if close > last_high.price and (last_high.index, True) not in broken:
                broken.add((last_high.index, True))
                kind = "BOS" if trend is Trend.UP else "CHOCH"
                breaks.append(StructureBreak(i, Direction.LONG, last_high.price, kind))
                trend = Trend.UP
        if lows:
            last_low = lows[-1]
            if close < last_low.price and (last_low.index, False) not in broken:
                broken.add((last_low.index, False))
                kind = "BOS" if trend is Trend.DOWN else "CHOCH"
                breaks.append(StructureBreak(i, Direction.SHORT, last_low.price, kind))
                trend = Trend.DOWN
    return breaks


def find_fair_value_gaps(
    candles: list[Candle], up_to: int | None = None, min_size: float = 0.0
) -> list[FairValueGap]:
    """Detect three-bar imbalances.

    The gap at pattern-centre ``i`` needs bar ``i + 1`` to exist, so the caller
    sees it from bar ``i + 1`` onward — the returned index is ``i + 1``, the bar
    at which it became visible, not the centre.
    """
    end = len(candles) - 1 if up_to is None else up_to
    gaps: list[FairValueGap] = []
    for i in range(1, min(end, len(candles) - 1)):
        prev, nxt = candles[i - 1], candles[i + 1]
        if nxt.low > prev.high and (nxt.low - prev.high) >= min_size:
            gaps.append(FairValueGap(i + 1, Direction.LONG, nxt.low, prev.high))
        elif prev.low > nxt.high and (prev.low - nxt.high) >= min_size:
            gaps.append(FairValueGap(i + 1, Direction.SHORT, prev.low, nxt.high))
    return gaps


def find_order_blocks(
    candles: list[Candle], breaks: list[StructureBreak], max_lookback: int = 12
) -> list[OrderBlock]:
    """Locate the last opposing candle before each structure break."""
    blocks: list[OrderBlock] = []
    for brk in breaks:
        start = max(0, brk.index - max_lookback)
        for i in range(brk.index - 1, start - 1, -1):
            candle = candles[i]
            wants_bearish = brk.direction is Direction.LONG
            if (wants_bearish and candle.is_bearish) or (
                not wants_bearish and candle.is_bullish
            ):
                blocks.append(
                    OrderBlock(i, brk.direction, candle.high, candle.low, confirmed_at=brk.index)
                )
                break
    return blocks


def find_liquidity_sweeps(
    candles: list[Candle], swings: list[Swing], up_to: int | None = None
) -> list[LiquiditySweep]:
    """Detect stop runs: a wick beyond a prior swing that closes back inside."""
    end = len(candles) - 1 if up_to is None else up_to
    sweeps: list[LiquiditySweep] = []
    for i in range(1, end + 1):
        candle = candles[i]
        visible = [s for s in swings_known_at(swings, i) if s.index < i]
        if not visible:
            continue
        for swing in reversed(visible[-8:]):
            if swing.is_high and candle.high > swing.price and candle.close < swing.price:
                sweeps.append(LiquiditySweep(i, Direction.SHORT, swing.price))
                break
            if swing.is_low and candle.low < swing.price and candle.close > swing.price:
                sweeps.append(LiquiditySweep(i, Direction.LONG, swing.price))
                break
    return sweeps


@dataclass(frozen=True)
class StructureView:
    """Everything structural that is knowable at one bar.

    Built once per decision bar so the strategy layer never has to remember to
    apply the confirmation filter itself.
    """

    index: int
    trend: Trend
    swings: tuple[Swing, ...]
    breaks: tuple[StructureBreak, ...]
    gaps: tuple[FairValueGap, ...]
    order_blocks: tuple[OrderBlock, ...]
    sweeps: tuple[LiquiditySweep, ...]

    @property
    def last_break(self) -> StructureBreak | None:
        return self.breaks[-1] if self.breaks else None

    @property
    def last_swing_high(self) -> Swing | None:
        highs = [s for s in self.swings if s.is_high]
        return highs[-1] if highs else None

    @property
    def last_swing_low(self) -> Swing | None:
        lows = [s for s in self.swings if s.is_low]
        return lows[-1] if lows else None


def build_view(candles: list[Candle], index: int, left: int = 2, right: int = 2) -> StructureView:
    """Assemble the structural picture available to a decision at ``index``.

    Only ``candles[: index + 1]`` is passed downstream, so a caller cannot
    accidentally hand a strategy the future even if it holds the full series.
    """
    if index < 0 or index >= len(candles):
        raise IndexError(f"index {index} out of range for {len(candles)} candles")
    visible = candles[: index + 1]
    swings = swings_known_at(swing_points(visible, left, right), index)
    breaks = detect_structure_breaks(visible, swings, up_to=index)
    gaps = find_fair_value_gaps(visible, up_to=index)
    blocks = find_order_blocks(visible, breaks)
    sweeps = find_liquidity_sweeps(visible, swings, up_to=index)
    return StructureView(
        index=index,
        trend=classify_trend(swings),
        swings=tuple(swings),
        breaks=tuple(breaks),
        gaps=tuple(gaps),
        order_blocks=tuple(blocks),
        sweeps=tuple(sweeps),
    )
