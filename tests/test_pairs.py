"""Win rate by pair.

Measuring sixty instruments introduces a failure the three-pair version could not
have: **selection**. Rank sixty win rates and the top row is, by construction,
the luckiest row — and it is the row the eye goes to. These tests pin the guards
that stop that row being sold as an edge:

* every pair asked about appears in the output, including the ones with no data,
  so the table cannot have been filtered on its own outcome;
* intervals widen with the number of pairs inspected;
* a pair with a handful of trades never earns a verdict, however good it looks;
* ranking cannot put a losing pair above a profitable one.
"""

from __future__ import annotations

import json
import math

import pytest

from trading_bot.config import Config
from trading_bot.data.synthetic import SyntheticSource
from trading_bot.metrics import family_confidence
from trading_bot.models import Timeframe
from trading_bot.pairs import (
    STATUS_MEASURED,
    STATUS_NO_DATA,
    analyse_universe,
    currency_breakdown,
    format_persistence,
    format_universe,
    persistence_check,
)

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]


@pytest.fixture
def candles():
    source = SyntheticSource()
    return {s: source.fetch(s, Timeframe.H1, 900) for s in SYMBOLS}


@pytest.fixture
def universe(candles, config):
    return analyse_universe(candles, config, split=None, symbols=SYMBOLS)


class TestCoverage:
    def test_every_pair_asked_about_is_reported(self, universe):
        assert {r.symbol for r in universe.reports} == set(SYMBOLS)

    def test_a_pair_with_no_data_is_reported_as_unmeasured_not_dropped(self, candles, config):
        """Silence would read as a clean bill of health. It is not one."""
        asked = SYMBOLS + ["EURNOK"]
        report = analyse_universe(candles, config, split=None, symbols=asked)
        missing = [r for r in report.reports if r.symbol == "EURNOK"]
        assert missing, "the pair must still appear"
        assert missing[0].status == STATUS_NO_DATA
        assert missing[0].verdict() == "NO DATA"
        assert "unmeasured, not cleared" in format_universe(report, config)

    def test_a_series_too_short_to_warm_the_indicators_is_not_measured(self, config):
        short = {"EURUSD": SyntheticSource().fetch("EURUSD", Timeframe.H1, 200)[:50]}
        report = analyse_universe(short, config, split=None, symbols=["EURUSD"])
        assert report.reports[0].status == STATUS_NO_DATA
        assert "not enough to warm" in report.reports[0].note


class TestMultipleComparisons:
    def test_the_correction_widens_every_interval(self, universe):
        for row in universe.measured:
            assert row.family_interval.low <= row.metrics.win_rate_interval.low
            assert row.family_interval.high >= row.metrics.win_rate_interval.high

    def test_more_pairs_means_a_stricter_per_pair_standard(self):
        assert family_confidence(0.95, 1) == 0.95
        assert family_confidence(0.95, 30) > family_confidence(0.95, 5) > 0.95

    def test_the_family_confidence_matches_the_number_of_pairs(self, universe, config):
        assert universe.family_conf == pytest.approx(
            family_confidence(config.target.confidence, len(SYMBOLS))
        )

    def test_a_verdict_requires_the_corrected_bound_not_the_raw_one(self, universe):
        for row in universe.tradable:
            assert row.family_interval.low > row.baseline


class TestSmallSamples:
    def test_a_single_trade_never_reads_trade_it(self, config):
        """The defect this test exists for was visible on screen, not in a total.

        One winning trade with no loser has no realised ratio, so the chance
        baseline falls back to the planned one — which a single win clears. The
        pair rendered as TRADE IT on a sample of one.
        """
        report = analyse_universe(
            {s: SyntheticSource().fetch(s, Timeframe.H1, 900) for s in SYMBOLS},
            config, split=0.7, symbols=SYMBOLS,
        )
        for row in report.reports:
            if 0 < row.metrics.trades < config.target.min_sample:
                assert row.verdict() != "TRADE IT"
                assert not row.survives_correction

    def test_an_unbounded_expectancy_bound_is_not_a_pass(self, config):
        """A one-trade sample gives an unbounded mean interval, never a positive one."""
        one = {"EURUSD": SyntheticSource().fetch("EURUSD", Timeframe.H1, 900)}
        report = analyse_universe(one, config, split=None, symbols=["EURUSD"])
        row = report.reports[0]
        if row.metrics.trades == 1:
            assert math.isinf(row.expectancy_interval.low)
            assert not row.profitable_lower_bound

    def test_a_thin_sample_is_labelled_thin(self, universe, config):
        for row in universe.measured:
            if row.metrics.trades < config.target.min_sample:
                assert row.status != STATUS_MEASURED


class TestRanking:
    def test_a_losing_pair_never_outranks_a_profitable_one(self, universe):
        """Low variance around a loss is not a recommendation.

        Ranking on the pessimistic bound alone would float a pair that lost -1R
        eight times running above one that made money unevenly, because a run of
        identical losses has almost no variance.
        """
        ranked = [r for r in universe.ranked() if r.has_trades]
        signs = [r.metrics.expectancy_r > 0 for r in ranked]
        assert signs == sorted(signs, reverse=True)

    def test_the_recommendation_never_names_a_losing_pair(self, universe, config):
        text = format_universe(universe, config)
        for row in universe.ranked():
            if row.has_trades and row.metrics.expectancy_r < 0:
                assert f"Best of the profitable ones is {row.symbol}" not in text


class TestCurrencyBreakdown:
    def test_each_trade_counts_under_both_its_legs(self, universe):
        rows = {r.code: r for r in universe.currencies}
        total_pair_trades = sum(r.metrics.trades for r in universe.measured)
        total_leg_trades = sum(r.trades for r in rows.values())
        assert total_leg_trades == total_pair_trades * 2

    def test_currencies_are_ranked_by_expectancy(self, universe):
        values = [r.expectancy_r for r in universe.currencies]
        assert values == sorted(values, reverse=True)

    def test_an_empty_universe_produces_no_currency_rows(self):
        assert currency_breakdown([]) == []


class TestReporting:
    def test_the_payload_is_strict_json(self, universe):
        """An unbounded interval must not serialise as a bare Infinity."""
        json.dumps(universe.to_dict(), allow_nan=False)

    def test_the_table_explains_the_correction_rather_than_just_applying_it(
        self, universe, config
    ):
        text = format_universe(universe, config)
        assert "corrected" in text
        assert "chance" in text
        assert "price of having looked at" in text

    def test_pooled_results_carry_the_dependence_caveat(self, universe, config):
        text = format_universe(universe, config)
        if not universe.pooled.is_empty:
            assert "not independent" in text or "move together" in text


class TestPersistence:
    """The walk-forward test of this module's own recommendation.

    "Trade the pairs that measured well" is a hypothesis, not a fact. These tests
    pin the mechanics of asking it — above all that the selection cannot see the
    period it is judged on.
    """

    def test_every_pair_lands_on_one_side_of_the_choice(self, candles, config):
        result = persistence_check(candles, config)
        assert set(result.selected) | set(result.rejected) == set(candles)
        assert not set(result.selected) & set(result.rejected)

    def test_the_pooled_total_is_the_two_subsets_together(self, candles, config):
        """If these disagreed, the comparison would be against a different universe."""
        result = persistence_check(candles, config)
        assert result.everything.trades == result.chosen.trades + result.dropped.trades

    def test_no_difference_is_reported_as_a_finding_not_a_failure(self, candles, config):
        result = persistence_check(candles, config)
        assert result.verdict() in (
            "SELECTION HELPED",
            "SELECTION HURT",
            "SELECTION MADE NO DIFFERENCE",
            "NOT TESTABLE",
        )
        assert result.verdict() in format_persistence(result)

    def test_a_thin_comparison_reaches_no_verdict(self, candles, config):
        """One chosen pair is not evidence that choosing pairs works."""
        result = persistence_check(candles, config)
        if not result.testable:
            assert result.verdict() == "NOT TESTABLE"
            assert not result.helped
            assert "Not enough trades on both sides" in format_persistence(result)

    def test_the_verdict_gate_uses_the_configured_minimum(self, candles, config):
        result = persistence_check(candles, config)
        assert result.min_verdict_trades == config.target.min_sample

    def test_a_series_too_short_to_split_is_skipped(self, config):
        tiny = {"EURUSD": SyntheticSource().fetch("EURUSD", Timeframe.H1, 100)}
        result = persistence_check(tiny, config)
        assert result.everything.trades == 0
        assert result.verdict() == "NOT TESTABLE"

    def test_the_gain_is_measured_on_the_later_half_only(self, candles, config):
        """The number quoted must be what selection bought, not what it was chosen on."""
        result = persistence_check(candles, config)
        assert result.gain_r == pytest.approx(
            result.chosen.expectancy_r - result.everything.expectancy_r
        )

    def test_the_report_states_the_method_not_just_the_answer(self, candles, config):
        text = format_persistence(persistence_check(candles, config))
        assert "split in half" in text
        assert "Nothing from the second half is visible" in text
        assert "chance alone gives 50%" in text
