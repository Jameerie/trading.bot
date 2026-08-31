"""Market structure, with particular attention to the confirmation delay.

The look-ahead tests here are the most important in the suite. If a swing point
becomes visible before the bars that define it have closed, every downstream
number — win rate above all — is fiction.
"""

from __future__ import annotations

import pytest

from trading_bot.models import Direction
from trading_bot.structure import (
    Trend,
    build_view,
    classify_trend,
    detect_structure_breaks,
    find_fair_value_gaps,
    find_liquidity_sweeps,
    find_order_blocks,
    swing_points,
    swings_known_at,
)

from conftest import make_candle, make_series


class TestSwingPoints:
    def test_finds_an_obvious_pivot_high(self):
        candles = make_series([1.10, 1.11, 1.15, 1.11, 1.10])
        highs = [s for s in swing_points(candles, 2, 2) if s.is_high]
        assert len(highs) == 1
        assert highs[0].index == 2

    def test_finds_an_obvious_pivot_low(self):
        candles = make_series([1.15, 1.14, 1.10, 1.14, 1.15])
        lows = [s for s in swing_points(candles, 2, 2) if s.is_low]
        assert len(lows) == 1
        assert lows[0].index == 2

    def test_monotonic_series_has_no_pivots(self):
        """A line has no swings. This is correct, and worth pinning down."""
        candles = make_series([1.10 + 0.001 * i for i in range(30)])
        assert swing_points(candles, 2, 2) == []

    def test_rejects_zero_window(self):
        with pytest.raises(ValueError):
            swing_points(make_series([1.1] * 10), 0, 2)


class TestLookAhead:
    """The confirmation delay is the load-bearing guarantee of this project."""

    def test_pivot_is_confirmed_after_its_right_bars(self):
        candles = make_series([1.10, 1.11, 1.15, 1.11, 1.10])
        swing = swing_points(candles, 2, 2)[0]
        assert swing.confirmed_at == swing.index + 2

    @pytest.mark.parametrize("right", [1, 2, 3])
    def test_confirmation_always_lags_the_pivot(self, right, random_series):
        for swing in swing_points(random_series, 2, right):
            assert swing.confirmed_at == swing.index + right
            assert swing.confirmed_at > swing.index

    def test_known_at_excludes_unconfirmed_pivots(self, random_series):
        swings = swing_points(random_series, 2, 2)
        for index in (50, 120, 300):
            for swing in swings_known_at(swings, index):
                assert swing.confirmed_at <= index

    def test_view_never_references_a_future_bar(self, random_series):
        """Nothing in a view built at bar i may point past bar i."""
        for index in (250, 400, 600):
            view = build_view(random_series, index)
            for swing in view.swings:
                assert swing.confirmed_at <= index
            for brk in view.breaks:
                assert brk.index <= index
            for gap in view.gaps:
                assert gap.index <= index
            for block in view.order_blocks:
                assert block.confirmed_at <= index
            for sweep in view.sweeps:
                assert sweep.index <= index

    def test_view_is_unchanged_by_appending_future_bars(self, random_series):
        """Truncating the future must not change the past.

        This is the test that would catch an accidental full-series peek: a view
        at bar 300 must be identical whether or not bars 301+ exist.
        """
        index = 300
        full = build_view(random_series, index)
        truncated = build_view(random_series[: index + 1], index)
        assert full.trend == truncated.trend
        assert [s.index for s in full.swings] == [s.index for s in truncated.swings]
        assert [b.index for b in full.breaks] == [b.index for b in truncated.breaks]
        assert [g.index for g in full.gaps] == [g.index for g in truncated.gaps]
        assert [o.index for o in full.order_blocks] == [o.index for o in truncated.order_blocks]
        assert [s.index for s in full.sweeps] == [s.index for s in truncated.sweeps]


class TestTrend:
    def test_higher_highs_and_lows_is_an_uptrend(self):
        candles = make_series([1.10, 1.12, 1.11, 1.14, 1.13, 1.17, 1.16, 1.20, 1.19, 1.24])
        assert classify_trend(swing_points(candles, 1, 1)) is Trend.UP

    def test_lower_highs_and_lows_is_a_downtrend(self):
        candles = make_series([1.24, 1.19, 1.20, 1.16, 1.17, 1.13, 1.14, 1.11, 1.12, 1.10])
        assert classify_trend(swing_points(candles, 1, 1)) is Trend.DOWN

    def test_mixed_structure_is_a_range(self):
        """Disagreeing highs and lows must not be called a trend."""
        candles = make_series([1.10, 1.14, 1.09, 1.15, 1.11, 1.13, 1.08, 1.14, 1.10, 1.13])
        assert classify_trend(swing_points(candles, 1, 1)) is Trend.RANGE

    def test_too_few_swings_is_a_range(self):
        assert classify_trend([]) is Trend.RANGE


class TestStructureBreaks:
    def test_first_break_against_a_range_is_a_choch(self):
        candles = make_series([1.10, 1.12, 1.11, 1.13, 1.12, 1.14, 1.13, 1.16])
        swings = swing_points(candles, 1, 1)
        breaks = detect_structure_breaks(candles, swings)
        assert breaks
        assert breaks[0].kind == "CHOCH"

    def test_continuation_breaks_are_bos(self):
        candles = make_series([1.10, 1.12, 1.11, 1.13, 1.12, 1.14, 1.13, 1.16, 1.15, 1.19])
        breaks = detect_structure_breaks(candles, swing_points(candles, 1, 1))
        kinds = [b.kind for b in breaks]
        assert "BOS" in kinds

    def test_a_level_only_breaks_once(self):
        candles = make_series([1.10, 1.12, 1.11, 1.13, 1.13, 1.13, 1.13])
        breaks = detect_structure_breaks(candles, swing_points(candles, 1, 1))
        levels = [(b.level, b.direction) for b in breaks]
        assert len(levels) == len(set(levels))


class TestFairValueGaps:
    def test_detects_a_bullish_gap(self):
        candles = [
            make_candle(0, 1.100, 1.102, 1.099, 1.101),
            make_candle(1, 1.101, 1.110, 1.101, 1.109),
            make_candle(2, 1.109, 1.115, 1.105, 1.112),  # low 1.105 > bar0 high 1.102
        ]
        gaps = find_fair_value_gaps(candles)
        assert len(gaps) == 1
        assert gaps[0].direction is Direction.LONG
        assert gaps[0].bottom == pytest.approx(1.102)
        assert gaps[0].top == pytest.approx(1.105)

    def test_detects_a_bearish_gap(self):
        candles = [
            make_candle(0, 1.110, 1.112, 1.108, 1.109),
            make_candle(1, 1.109, 1.109, 1.100, 1.101),
            make_candle(2, 1.101, 1.105, 1.098, 1.099),  # high 1.105 < bar0 low 1.108
        ]
        gaps = find_fair_value_gaps(candles)
        assert len(gaps) == 1
        assert gaps[0].direction is Direction.SHORT

    def test_gap_is_indexed_at_the_bar_that_revealed_it(self):
        candles = [
            make_candle(0, 1.100, 1.102, 1.099, 1.101),
            make_candle(1, 1.101, 1.110, 1.101, 1.109),
            make_candle(2, 1.109, 1.115, 1.105, 1.112),
        ]
        # The pattern centres on bar 1 but is only knowable once bar 2 closes.
        assert find_fair_value_gaps(candles)[0].index == 2

    def test_no_gap_when_bars_overlap(self):
        candles = make_series([1.100, 1.101, 1.102], wick=0.002)
        assert find_fair_value_gaps(candles) == []


class TestOrderBlocks:
    def test_block_records_the_break_that_revealed_it(self):
        candles = make_series([1.10, 1.12, 1.11, 1.13, 1.12, 1.14, 1.13, 1.17])
        breaks = detect_structure_breaks(candles, swing_points(candles, 1, 1))
        for block in find_order_blocks(candles, breaks):
            assert block.confirmed_at > block.index


class TestLiquiditySweeps:
    def test_wick_through_a_high_that_closes_back_is_a_sweep(self):
        candles = [
            make_candle(0, 1.100, 1.104, 1.099, 1.103),
            make_candle(1, 1.103, 1.108, 1.102, 1.107),  # pivot high at 1.108
            make_candle(2, 1.100, 1.103, 1.099, 1.101),  # confirms the pivot
            make_candle(3, 1.101, 1.112, 1.100, 1.105),  # pierces 1.108, closes back below
            make_candle(4, 1.105, 1.106, 1.100, 1.101),
        ]
        swings = swing_points(candles, 1, 1)
        # The pivot must be confirmed before the sweep bar, or it is invisible.
        assert any(s.is_high and s.confirmed_at <= 3 for s in swings)
        sweeps = find_liquidity_sweeps(candles, swings)
        assert any(s.direction is Direction.SHORT and s.index == 3 for s in sweeps)

    def test_sweep_needs_a_pivot_confirmed_before_it(self):
        """A wick past a level that is not yet a confirmed pivot is not a sweep."""
        candles = [
            make_candle(0, 1.100, 1.104, 1.099, 1.103),
            make_candle(1, 1.103, 1.108, 1.102, 1.107),
            make_candle(2, 1.107, 1.112, 1.100, 1.105),  # pierces before bar 1 is confirmed
        ]
        swings = swing_points(candles, 1, 1)
        assert not [s for s in find_liquidity_sweeps(candles, swings) if s.index == 2]

    def test_clean_break_is_not_a_sweep(self):
        """Closing beyond the level is a break, not a rejection."""
        candles = make_series([1.100, 1.104, 1.101, 1.108, 1.112, 1.116])
        swings = swing_points(candles, 1, 1)
        for sweep in find_liquidity_sweeps(candles, swings):
            candle = candles[sweep.index]
            if sweep.direction is Direction.SHORT:
                assert candle.close < sweep.swept_level
            else:
                assert candle.close > sweep.swept_level


class TestBuildView:
    def test_rejects_out_of_range_index(self, random_series):
        with pytest.raises(IndexError):
            build_view(random_series, len(random_series))
        with pytest.raises(IndexError):
            build_view(random_series, -1)
