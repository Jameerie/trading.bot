"""Indicator correctness.

Every indicator is checked for two things: the right value, and the right
*alignment*. A correct value at the wrong index is a look-ahead bug.
"""

from __future__ import annotations

import pytest

from trading_bot import indicators as ind
from trading_bot.models import Candle

from conftest import make_series


class TestAlignment:
    def test_every_series_matches_input_length(self, random_series):
        n = len(random_series)
        closes = ind.closes(random_series)
        for series in (
            ind.sma(closes, 10),
            ind.ema(closes, 10),
            ind.rma(closes, 10),
            ind.rsi(closes, 14),
            ind.atr(random_series, 14),
            ind.adx(random_series, 14),
            ind.true_range(random_series),
        ):
            assert len(series) == n

    def test_warmup_is_none_not_zero(self):
        values = [float(i) for i in range(50)]
        result = ind.sma(values, 10)
        assert result[:9] == [None] * 9
        assert result[9] is not None

    def test_short_input_returns_all_none(self):
        assert ind.ema([1.0, 2.0], 10) == [None, None]
        assert ind.sma([1.0, 2.0], 10) == [None, None]


class TestSma:
    def test_known_value(self):
        assert ind.sma([1.0, 2.0, 3.0, 4.0], 2)[-1] == pytest.approx(3.5)

    def test_constant_series_equals_constant(self):
        assert ind.sma([5.0] * 20, 5)[-1] == pytest.approx(5.0)


class TestEma:
    def test_seeded_with_sma(self):
        values = [float(i) for i in range(1, 21)]
        result = ind.ema(values, 5)
        # First emitted value is the SMA of the first window: (1+2+3+4+5)/5.
        assert result[4] == pytest.approx(3.0)

    def test_recursion_matches_manual(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        result = ind.ema(values, 3)
        alpha = 2 / 4
        expected = 2.0  # SMA of 1,2,3
        for v in values[3:]:
            expected = v * alpha + expected * (1 - alpha)
        assert result[-1] == pytest.approx(expected)

    def test_tracks_a_constant(self):
        assert ind.ema([7.0] * 30, 10)[-1] == pytest.approx(7.0)


class TestRsi:
    def test_monotonic_rise_is_100(self):
        values = [float(i) for i in range(1, 40)]
        assert ind.rsi(values, 14)[-1] == pytest.approx(100.0)

    def test_monotonic_fall_is_0(self):
        values = [float(i) for i in range(40, 1, -1)]
        assert ind.rsi(values, 14)[-1] == pytest.approx(0.0)

    def test_stays_within_bounds(self, random_series):
        for value in ind.rsi(ind.closes(random_series), 14):
            if value is not None:
                assert 0.0 <= value <= 100.0


class TestAtr:
    def test_first_bar_has_no_true_range(self, random_series):
        assert ind.true_range(random_series)[0] is None

    def test_true_range_covers_gaps(self):
        from conftest import make_candle

        candles = [
            make_candle(0, 1.10, 1.11, 1.09, 1.10),
            make_candle(1, 1.20, 1.21, 1.19, 1.20),  # gaps up from 1.10
        ]
        # Range of bar 2 alone is 0.02, but the gap from the prior close is 0.11.
        assert ind.true_range(candles)[1] == pytest.approx(0.11)

    def test_atr_is_positive(self, random_series):
        for value in ind.atr(random_series, 14):
            if value is not None:
                assert value > 0


class TestAdx:
    def test_returns_none_when_too_short(self):
        assert all(v is None for v in ind.adx(make_series([1.1] * 10), 14))

    def test_stronger_on_a_trend_than_on_a_range(self):
        """A trending market should read higher than a ranging one.

        Both series come from the generator rather than being hand-built. A
        hand-made alternating series ends up with identical highs and lows on
        every bar, which produces no directional movement at all and makes ADX
        read 100 — a property of the fixture, not of the market it stands for.
        """
        from trading_bot.data.synthetic import generate, generate_trending

        trend = generate_trending(bars=400, seed=3)
        chop = generate(bars=400, seed=3, trend_strength=0.0, regime_bars=10_000)

        trend_adx = [v for v in ind.adx(trend, 14) if v is not None]
        chop_adx = [v for v in ind.adx(chop, 14) if v is not None]
        trend_mean = sum(trend_adx) / len(trend_adx)
        chop_mean = sum(chop_adx) / len(chop_adx)
        assert trend_mean > chop_mean

    def test_is_bounded(self, random_series):
        for value in ind.adx(random_series, 14):
            if value is not None:
                assert 0.0 <= value <= 100.0


class TestHelpers:
    def test_rolling_extreme(self):
        values = [1.0, 5.0, 3.0, 2.0]
        assert ind.rolling_extreme(values, 2, "max") == [None, 5.0, 5.0, 3.0]
        assert ind.rolling_extreme(values, 2, "min") == [None, 1.0, 3.0, 2.0]

    def test_slope_sign_follows_direction(self):
        rising = ind.slope([float(i) for i in range(20)], 5)
        assert rising[-1] == pytest.approx(1.0)
        falling = ind.slope([float(-i) for i in range(20)], 5)
        assert falling[-1] == pytest.approx(-1.0)

    def test_rejects_bad_period(self):
        with pytest.raises(ValueError):
            ind.sma([1.0, 2.0], 0)
