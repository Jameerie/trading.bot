"""Equivalence of the cached and uncached evaluation paths.

The cache exists for speed, and speed is never a reason to change results. These
tests are the licence for that optimisation: if they fail, the cache is wrong and
the slice-based path in ``build_context`` is the source of truth.
"""

from __future__ import annotations

import pytest

from trading_bot.precompute import build_cache
from trading_bot.resample import htf_closed_before
from trading_bot.scanner import evaluate_at, htf_bias
from trading_bot.strategy import get_strategy
from trading_bot.structure import build_view


@pytest.fixture
def warmup(config):
    return max(config.strategy.ema_trend, config.strategy.adx_period * 3) + 5


class TestViewEquivalence:
    def test_cached_view_matches_recomputed_view(self, random_series, config, warmup):
        cache = build_cache(random_series, config)
        for index in range(warmup, len(random_series), 37):
            cached = cache.view_at(index)
            fresh = build_view(
                random_series[: index + 1],
                index,
                config.strategy.swing_left,
                config.strategy.swing_right,
            )
            assert cached.trend == fresh.trend, f"trend differs at {index}"
            assert [s.index for s in cached.swings] == [s.index for s in fresh.swings]
            assert [b.index for b in cached.breaks] == [b.index for b in fresh.breaks]
            assert [b.kind for b in cached.breaks] == [b.kind for b in fresh.breaks]
            assert [g.index for g in cached.gaps] == [g.index for g in fresh.gaps]
            assert [o.index for o in cached.order_blocks] == [
                o.index for o in fresh.order_blocks
            ]
            assert [s.index for s in cached.sweeps] == [s.index for s in fresh.sweeps]


class TestHtfEquivalence:
    def test_cached_htf_trend_matches_recomputed(self, random_series, config, warmup):
        cache = build_cache(random_series, config)
        for index in range(warmup, len(random_series), 23):
            assert cache.htf_trend[index] == htf_bias(random_series, index, config), (
                f"HTF bias differs at {index}"
            )

    def test_htf_bars_have_all_closed_before_the_decision(self, random_series, config):
        from trading_bot.models import Timeframe

        target = Timeframe.parse(config.data.htf_timeframe)
        for index in (300, 500, 700):
            decision = random_series[index].timestamp
            for bar in htf_closed_before(random_series, index, target):
                assert bar.timestamp < decision


class TestSignalEquivalence:
    def test_every_bar_produces_the_same_signal(self, random_series, config, warmup):
        """The headline guarantee: caching changes nothing a user would see."""
        strategy = get_strategy(config.strategy.name)
        cache = build_cache(random_series, config)
        mismatches = []
        for index in range(warmup, len(random_series)):
            slow = evaluate_at(random_series, index, "EURUSD", config, strategy)
            fast = evaluate_at(random_series, index, "EURUSD", config, strategy, cache)
            if (slow.signal is None) != (fast.signal is None):
                mismatches.append(index)
            elif slow.signal is not None and slow.signal.to_dict() != fast.signal.to_dict():
                mismatches.append(index)
            elif slow.confluence_fraction != fast.confluence_fraction:
                mismatches.append(index)
        assert not mismatches, f"cached and uncached disagree at bars {mismatches[:10]}"

    def test_trending_series_also_agrees(self, trending_series, config, warmup):
        strategy = get_strategy(config.strategy.name)
        cache = build_cache(trending_series, config)
        for index in range(warmup, len(trending_series), 11):
            slow = evaluate_at(trending_series, index, "EURUSD", config, strategy)
            fast = evaluate_at(trending_series, index, "EURUSD", config, strategy, cache)
            assert (slow.signal is None) == (fast.signal is None)
            if slow.signal is not None:
                assert slow.signal.to_dict() == fast.signal.to_dict()


class TestCacheIsCausal:
    def test_truncating_the_future_does_not_change_the_past(self, random_series, config):
        """A cache built on more data must still describe the past identically.

        This is the test that would catch a filter keyed on the wrong index — an
        order block discovered at bar 500 leaking into a decision at bar 300.
        """
        index = 300
        full = build_cache(random_series, config)
        partial = build_cache(random_series[: index + 1], config)

        assert full.ema_fast[index] == pytest.approx(partial.ema_fast[index])
        assert full.ema_slow[index] == pytest.approx(partial.ema_slow[index])
        assert full.atr[index] == pytest.approx(partial.atr[index])
        assert full.rsi[index] == pytest.approx(partial.rsi[index])
        assert full.adx[index] == pytest.approx(partial.adx[index])
        assert full.htf_trend[index] == partial.htf_trend[index]

        a, b = full.view_at(index), partial.view_at(index)
        assert a.trend == b.trend
        assert [s.index for s in a.swings] == [s.index for s in b.swings]
        assert [o.index for o in a.order_blocks] == [o.index for o in b.order_blocks]
        assert [g.index for g in a.gaps] == [g.index for g in b.gaps]

    def test_order_blocks_are_gated_on_confirmation_not_position(self, random_series, config):
        cache = build_cache(random_series, config)
        for index in (250, 400, 600):
            for block in cache.view_at(index).order_blocks:
                assert block.confirmed_at <= index
