"""Metrics and the quality gate.

The gate is what stops this project from claiming an 85% win rate it has not
earned, so it gets tested hard.
"""

from __future__ import annotations

import math

import pytest

from trading_bot.metrics import (
    Interval,
    compute_metrics,
    equity_curve,
    evaluate_gate,
    family_confidence,
    longest_streak,
    max_drawdown,
    mean_interval,
    wilson_interval,
    _z_for,
)
from trading_bot.models import Outcome, Trade

from conftest import START
from test_backtest import make_signal


def make_trade(r: float, outcome: Outcome | None = None, bars: int = 10) -> Trade:
    if outcome is None:
        outcome = Outcome.WIN if r > 0 else Outcome.LOSS
    return Trade(
        signal=make_signal(),
        entry_time=START,
        exit_time=START,
        exit_price=1.1,
        outcome=outcome,
        r_multiple=r,
        bars_held=bars,
    )


class TestWilsonInterval:
    @pytest.mark.parametrize(
        "confidence,expected", [(0.95, 1.9600), (0.99, 2.5758), (0.90, 1.6449)]
    )
    def test_critical_values(self, confidence, expected):
        assert _z_for(confidence) == pytest.approx(expected, abs=0.0005)

    def test_known_interval(self):
        """85 of 100 -> roughly 76.7%-90.7%, a standard textbook result."""
        interval = wilson_interval(85, 100)
        assert interval.low == pytest.approx(0.767, abs=0.005)
        assert interval.high == pytest.approx(0.907, abs=0.005)

    def test_small_samples_stay_wide(self):
        """7 of 8 is 87.5% and means almost nothing. The interval must say so."""
        interval = wilson_interval(7, 8)
        assert interval.low < 0.6
        assert interval.high > 0.9

    def test_more_evidence_narrows_the_interval(self):
        small = wilson_interval(17, 20)
        large = wilson_interval(850, 1000)
        assert (large.high - large.low) < (small.high - small.low)

    def test_extremes_stay_in_bounds(self):
        for successes, total in [(0, 10), (10, 10), (0, 1), (1, 1)]:
            interval = wilson_interval(successes, total)
            assert 0.0 <= interval.low <= interval.high <= 1.0

    def test_no_data_is_maximally_uncertain(self):
        interval = wilson_interval(0, 0)
        assert interval.low == 0.0 and interval.high == 1.0


class TestComputeMetrics:
    def test_empty_input(self):
        metrics = compute_metrics([])
        assert metrics.is_empty
        assert metrics.trades == 0

    def test_counts_and_rates(self):
        trades = [make_trade(4.0), make_trade(-1.0), make_trade(-1.0), make_trade(4.0)]
        metrics = compute_metrics(trades)
        assert metrics.trades == 4
        assert metrics.wins == 2
        assert metrics.losses == 2
        assert metrics.win_rate == pytest.approx(0.5)
        assert metrics.total_r == pytest.approx(6.0)
        assert metrics.expectancy_r == pytest.approx(1.5)

    def test_profit_factor(self):
        trades = [make_trade(4.0), make_trade(-1.0), make_trade(-1.0)]
        assert compute_metrics(trades).profit_factor == pytest.approx(2.0)

    def test_expired_trades_are_counted_not_dropped(self):
        """An expired trade has a real P&L and must not be quietly excluded."""
        trades = [make_trade(4.0), make_trade(-0.5, Outcome.EXPIRED)]
        metrics = compute_metrics(trades)
        assert metrics.trades == 2
        assert metrics.expired == 1
        assert metrics.total_r == pytest.approx(3.5)

    def test_a_profitable_expired_trade_counts_as_a_win(self):
        metrics = compute_metrics([make_trade(0.8, Outcome.EXPIRED)])
        assert metrics.wins == 1

    def test_averages(self):
        metrics = compute_metrics([make_trade(4.0), make_trade(2.0), make_trade(-1.0)])
        assert metrics.average_win_r == pytest.approx(3.0)
        assert metrics.average_loss_r == pytest.approx(-1.0)


class TestDrawdownAndStreaks:
    def test_drawdown(self):
        assert max_drawdown([1.0, -1.0, -1.0, -1.0, 4.0]) == pytest.approx(3.0)

    def test_no_drawdown_when_only_rising(self):
        assert max_drawdown([1.0, 1.0, 1.0]) == pytest.approx(0.0)

    def test_streaks(self):
        trades = [make_trade(r) for r in (1, 1, -1, -1, -1, 1)]
        assert longest_streak(trades, win=True) == 2
        assert longest_streak(trades, win=False) == 3

    def test_equity_curve_accumulates(self):
        assert equity_curve([make_trade(1.0), make_trade(-0.5)]) == [1.0, 0.5]


class TestQualityGate:
    def test_small_sample_never_passes(self):
        """Ten straight wins is not evidence of an 85% strategy."""
        metrics = compute_metrics([make_trade(4.0)] * 10)
        gate = evaluate_gate(metrics, 0.85, min_sample=30)
        assert not gate.passed
        assert gate.verdict == "INSUFFICIENT DATA"

    def test_point_estimate_above_target_is_only_unproven(self):
        """Above the line but with a wide interval must not be reported as a pass."""
        trades = [make_trade(4.0)] * 27 + [make_trade(-1.0)] * 3  # 90% of 30
        gate = evaluate_gate(compute_metrics(trades), 0.85, min_sample=30)
        assert not gate.passed
        assert gate.verdict == "UNPROVEN"

    def test_large_convincing_sample_passes(self):
        trades = [make_trade(4.0)] * 940 + [make_trade(-1.0)] * 60  # 94% of 1000
        gate = evaluate_gate(compute_metrics(trades), 0.85, min_sample=30)
        assert gate.passed
        assert gate.verdict == "MEETS TARGET"

    def test_below_target_is_reported_plainly(self):
        trades = [make_trade(4.0)] * 10 + [make_trade(-1.0)] * 40  # 20% of 50
        gate = evaluate_gate(compute_metrics(trades), 0.85, min_sample=30)
        assert not gate.passed
        assert gate.verdict == "BELOW TARGET"

    def test_below_target_still_reports_positive_expectancy(self):
        """A 20% win rate at 1:4 is profitable, and the message should say so."""
        trades = [make_trade(4.0)] * 15 + [make_trade(-1.0)] * 35
        gate = evaluate_gate(compute_metrics(trades), 0.85, min_sample=30)
        assert "still profitable" in gate.detail

    def test_gate_tests_the_lower_bound_not_the_estimate(self):
        """Identical win rate, different evidence: only the larger sample passes."""
        small = compute_metrics([make_trade(4.0)] * 36 + [make_trade(-1.0)] * 4)
        large = compute_metrics([make_trade(4.0)] * 900 + [make_trade(-1.0)] * 100)
        assert small.win_rate == pytest.approx(0.90)
        assert large.win_rate == pytest.approx(0.90)
        assert not evaluate_gate(small, 0.85, 30).passed
        assert evaluate_gate(large, 0.85, 30).passed

    def test_sitting_exactly_on_the_target_cannot_pass(self):
        """A sample landing exactly on the line never proves it clears the line.

        850 wins from 1000 is precisely 85%, so half the interval lies below the
        target no matter how large the sample. Passing this would mean the gate
        was reading the point estimate.
        """
        metrics = compute_metrics([make_trade(4.0)] * 850 + [make_trade(-1.0)] * 150)
        gate = evaluate_gate(metrics, 0.85, min_sample=30)
        assert metrics.win_rate == pytest.approx(0.85)
        assert not gate.passed
        assert gate.verdict == "UNPROVEN"


class TestMeanInterval:
    """Win rate has Wilson; expectancy needs a bound of its own."""

    def test_a_single_observation_is_unbounded_not_precise(self):
        """The defect this pins: (mean, mean) let one trade pass any test put to it.

        A pair with one winning trade rendered as TRADE IT, because a zero-width
        interval around +4.5R cleared every lower-bound check in the codebase.
        One observation says nothing about the precision of a mean, and the
        interval now says so.
        """
        interval = mean_interval([4.0])
        assert math.isinf(interval.low) and interval.low < 0
        assert math.isinf(interval.high)

    def test_an_empty_sample_is_zero_width_at_zero(self):
        assert mean_interval([]) == Interval(0.0, 0.0, 0.95)

    def test_the_interval_brackets_the_mean(self):
        values = [4.0, -1.0, -1.0, -1.0, 4.0, -1.0, -1.0, -1.0]
        interval = mean_interval(values)
        mean = sum(values) / len(values)
        assert interval.low < mean < interval.high

    def test_more_data_narrows_it(self):
        few = mean_interval([4.0, -1.0] * 5)
        many = mean_interval([4.0, -1.0] * 50)
        assert (many.high - many.low) < (few.high - few.low)

    def test_it_is_not_clamped_to_a_proportion(self):
        """R-multiples are not probabilities; a negative bound is meaningful."""
        assert mean_interval([-1.0] * 10 + [-0.9]).low < 0


class TestFamilyConfidence:
    """Looking at sixty pairs and picking one is not a 95% question any more."""

    def test_one_comparison_changes_nothing(self):
        assert family_confidence(0.95, 1) == 0.95
        assert family_confidence(0.95, 0) == 0.95

    def test_more_comparisons_demand_a_stricter_standard(self):
        assert family_confidence(0.95, 60) > family_confidence(0.95, 10) > 0.95

    def test_it_stays_below_certainty(self):
        assert family_confidence(0.95, 1000) < 1.0

    def test_sidak_is_the_stated_formula(self):
        assert family_confidence(0.95, 4) == pytest.approx(0.95 ** 0.25)

    def test_the_corrected_interval_is_wider(self):
        raw = wilson_interval(8, 30, 0.95)
        corrected = wilson_interval(8, 30, family_confidence(0.95, 30))
        assert corrected.low < raw.low
        assert corrected.high > raw.high
