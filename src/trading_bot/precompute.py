"""Causal series cache.

Evaluating every bar by recomputing indicators and structure over the visible
slice is obviously correct but quadratic, which made ``calibrate`` unusable on a
few thousand bars.

The fix relies on one property: **every quantity this system uses is causal.**
An EMA, ATR, RSI or ADX value at bar *i* depends only on bars up to *i*. A swing
point carries the bar at which it became confirmed. A structure break is a
causal fold over the bars before it. So computing each series once over the full
history and then *indexing* at *i* yields exactly the value the slice-based path
produced — no future information can flow backwards through a causal function.

What this does **not** license is filtering on the wrong key. An order block
discovered at bar 50 sits at bar 5; gating it on its own index would leak it
into decisions made at bar 10. Everything here is therefore gated on the index
at which it became *knowable* (``confirmed_at``), not the index it refers to.

``tests/test_precompute.py`` asserts cached and uncached evaluation produce
identical signals. If that test fails, this optimisation is wrong and the naive
path is the source of truth.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

from . import indicators as ind
from .config import Config
from .models import Candle, Timeframe
from .resample import resample
from .structure import (
    FairValueGap,
    LiquiditySweep,
    OrderBlock,
    StructureBreak,
    StructureView,
    Swing,
    Trend,
    classify_trend,
    detect_structure_breaks,
    find_fair_value_gaps,
    find_liquidity_sweeps,
    find_order_blocks,
    swing_points,
    swings_known_at,
)

Series = list[float | None]


@dataclass(frozen=True)
class SeriesCache:
    """Indicators and structure for one series, indexable by bar.

    Each structural list is stored alongside a sorted list of its visibility
    keys, so a lookup is a binary search rather than a scan.
    """

    ema_fast: Series
    ema_slow: Series
    ema_trend: Series
    ema_fast_slope: Series
    atr: Series
    rsi: Series
    adx: Series
    plus_di: Series
    minus_di: Series
    swings: list[Swing]
    breaks: list[StructureBreak]
    gaps: list[FairValueGap]
    order_blocks: list[OrderBlock]
    sweeps: list[LiquiditySweep]
    htf_trend: list[Trend]
    _swing_keys: list[int] = field(default_factory=list)
    _break_keys: list[int] = field(default_factory=list)
    _gap_keys: list[int] = field(default_factory=list)
    _block_keys: list[int] = field(default_factory=list)
    _sweep_keys: list[int] = field(default_factory=list)

    def view_at(self, index: int) -> StructureView:
        """The structural picture knowable at ``index``."""
        swings = self.swings[: bisect_right(self._swing_keys, index)]
        return StructureView(
            index=index,
            trend=classify_trend(swings),
            swings=tuple(swings),
            breaks=tuple(self.breaks[: bisect_right(self._break_keys, index)]),
            gaps=tuple(self.gaps[: bisect_right(self._gap_keys, index)]),
            order_blocks=tuple(self.order_blocks[: bisect_right(self._block_keys, index)]),
            sweeps=tuple(self.sweeps[: bisect_right(self._sweep_keys, index)]),
        )


def _htf_trend_by_bar(candles: list[Candle], config: Config) -> list[Trend]:
    """Higher-timeframe trend for every bar, using only closed HTF bars.

    Walks the two series together with a single moving pointer: an HTF bar
    becomes visible at the first LTF bar whose timestamp is at or after the HTF
    bar's close. This reproduces ``resample.htf_closed_before`` without redoing
    the aggregation for each bar.
    """
    trends = [Trend.RANGE] * len(candles)
    try:
        target = Timeframe.parse(config.data.htf_timeframe)
    except Exception:
        return trends

    htf = resample(candles, target)
    if len(htf) < 6:
        return trends

    htf_swings = swing_points(htf, config.strategy.swing_left, config.strategy.swing_right)
    # Trend after each HTF bar closes, computed once per HTF bar.
    trend_after = [
        classify_trend(swings_known_at(htf_swings, k)) for k in range(len(htf))
    ]

    step_minutes = target.minutes
    close_times = [
        c.timestamp.timestamp() + step_minutes * 60 for c in htf
    ]

    pointer = 0
    for i, candle in enumerate(candles):
        stamp = candle.timestamp.timestamp()
        while pointer < len(close_times) and close_times[pointer] <= stamp:
            pointer += 1
        # `pointer` HTF bars have closed; require the same 6-bar minimum the
        # uncached path applies before trusting a trend reading.
        trends[i] = trend_after[pointer - 1] if pointer >= 6 else Trend.RANGE
    return trends


def build_cache(candles: list[Candle], config: Config) -> SeriesCache:
    """Compute every derived series once for the whole history."""
    s = config.strategy
    closes = ind.closes(candles)

    fast = ind.ema(closes, s.ema_fast)
    plus_di, minus_di = ind.directional_index(candles, s.adx_period)

    swings = swing_points(candles, s.swing_left, s.swing_right)
    breaks = detect_structure_breaks(candles, swings)
    gaps = find_fair_value_gaps(candles)
    blocks = find_order_blocks(candles, breaks)
    sweeps = find_liquidity_sweeps(candles, swings)

    return SeriesCache(
        ema_fast=fast,
        ema_slow=ind.ema(closes, s.ema_slow),
        ema_trend=ind.ema(closes, s.ema_trend),
        ema_fast_slope=ind.slope(fast, 5),
        atr=ind.atr(candles, s.atr_period),
        rsi=ind.rsi(closes, s.rsi_period),
        adx=ind.adx(candles, s.adx_period),
        plus_di=plus_di,
        minus_di=minus_di,
        swings=swings,
        breaks=breaks,
        gaps=gaps,
        order_blocks=blocks,
        sweeps=sweeps,
        htf_trend=_htf_trend_by_bar(candles, config),
        _swing_keys=[x.confirmed_at for x in swings],
        _break_keys=[x.index for x in breaks],
        _gap_keys=[x.index for x in gaps],
        _block_keys=[x.confirmed_at for x in blocks],
        _sweep_keys=[x.index for x in sweeps],
    )
