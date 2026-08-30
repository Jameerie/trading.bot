"""Position sizing analysis.

The properties tested here are mathematical, so they can be asserted exactly
rather than eyeballed: breakeven, Kelly, and the fact that growth peaks at Kelly
and falls beyond it.
"""

from __future__ import annotations

import pytest

from trading_bot.metrics import compute_metrics
from trading_bot.risk_analysis import (
    CONSERVATIVE_KELLY_FRACTION,
    analyse,
    analyse_from_metrics,
    breakeven_win_rate,
    expectancy_r,
    format_report,
    kelly_fraction,
    log_growth_rate,
    misestimation_grid,
    simulate_sizing,
)

from test_metrics import make_trade


class TestExpectancy:
    @pytest.mark.parametrize("reward,breakeven", [(1.0, 0.5), (3.0, 0.25), (4.0, 0.2), (9.0, 0.1)])
    def test_breakeven(self, reward, breakeven):
        assert breakeven_win_rate(reward) == pytest.approx(breakeven)

    def test_expectancy_is_zero_at_breakeven(self):
        assert expectancy_r(breakeven_win_rate(4.0), 4.0) == pytest.approx(0.0)

    def test_known_expectancies_at_four_to_one(self):
        assert expectancy_r(0.30, 4.0) == pytest.approx(0.50)
        assert expectancy_r(0.25, 4.0) == pytest.approx(0.25)
        assert expectancy_r(0.15, 4.0) == pytest.approx(-0.25)

    def test_rejects_a_non_positive_reward(self):
        with pytest.raises(ValueError):
            breakeven_win_rate(0)


class TestKelly:
    def test_known_values(self):
        assert kelly_fraction(0.30, 4.0) == pytest.approx(0.125)
        assert kelly_fraction(0.25, 4.0) == pytest.approx(0.0625)
        assert kelly_fraction(0.40, 4.0) == pytest.approx(0.25)

    def test_zero_at_and_below_breakeven(self):
        assert kelly_fraction(0.20, 4.0) == pytest.approx(0.0)
        assert kelly_fraction(0.10, 4.0) == 0.0

    def test_growth_is_maximised_at_kelly(self):
        """The defining property. If this fails, the formula is wrong."""
        win_rate, reward = 0.30, 4.0
        optimal = kelly_fraction(win_rate, reward)
        best = log_growth_rate(win_rate, optimal, reward)
        for delta in (-0.05, -0.02, 0.02, 0.05):
            other = optimal + delta
            if 0 < other < 1:
                assert log_growth_rate(win_rate, other, reward) < best

    def test_growth_turns_negative_well_past_kelly(self):
        assert log_growth_rate(0.30, 0.45, 4.0) < 0


class TestSimulation:
    def test_is_deterministic(self):
        a = simulate_sizing(0.30, 0.02, 4.0, trades=40, trials=300)
        b = simulate_sizing(0.30, 0.02, 4.0, trades=40, trials=300)
        assert a.median_multiple == b.median_multiple
        assert a.median_drawdown == b.median_drawdown

    def test_bigger_risk_means_bigger_drawdown(self):
        small = simulate_sizing(0.30, 0.01, 4.0, trades=60, trials=800)
        large = simulate_sizing(0.30, 0.08, 4.0, trades=60, trials=800)
        assert large.median_drawdown > small.median_drawdown
        assert large.prob_lose_half >= small.prob_lose_half

    def test_percentiles_are_ordered(self):
        row = simulate_sizing(0.30, 0.02, 4.0, trades=60, trials=800)
        assert row.p05_multiple <= row.median_multiple <= row.p95_multiple

    def test_drawdown_is_a_fraction(self):
        row = simulate_sizing(0.30, 0.05, 4.0, trades=60, trials=500)
        assert 0.0 <= row.median_drawdown <= 1.0

    def test_rejects_impossible_risk(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with pytest.raises(ValueError):
                simulate_sizing(0.30, bad, 4.0)


class TestAnalyse:
    def test_negative_edge_recommends_nothing(self):
        report = analyse(0.15, 4.0, trials=200)
        assert not report.is_profitable
        assert report.recommended_risk == 0.0
        assert report.rows == ()
        assert "loses money" in report.caution

    def test_recommendation_is_a_fraction_of_kelly(self):
        report = analyse(0.30, 4.0, trials=200)
        expected = report.kelly * CONSERVATIVE_KELLY_FRACTION
        assert report.recommended_risk == pytest.approx(expected, abs=0.0002)

    def test_recommendation_is_capped(self):
        """Even a huge edge must not recommend an account-ending size."""
        report = analyse(0.80, 4.0, trials=200, max_risk=0.05)
        assert report.recommended_risk <= 0.05

    def test_recommendation_never_exceeds_kelly(self):
        for rate in (0.22, 0.30, 0.45, 0.60):
            report = analyse(rate, 4.0, trials=200)
            assert report.recommended_risk <= report.kelly + 1e-9

    def test_rows_are_labelled(self):
        report = analyse(0.30, 4.0, trials=200)
        labels = {row.label for row in report.rows}
        assert "recommended" in labels
        assert any("Kelly" in label for label in labels if label)

    def test_report_renders(self):
        text = format_report(analyse(0.30, 4.0, trials=200))
        assert "RECOMMENDED RISK" in text
        assert "Breakeven" in text

    def test_losing_report_renders_without_a_table(self):
        text = format_report(analyse(0.10, 4.0, trials=200))
        assert "loses money" in text
        assert "RECOMMENDED RISK" not in text


class TestFromMetrics:
    def test_uses_the_lower_bound_by_default(self):
        """Sizing off the sample mean bets that the sample was not lucky."""
        metrics = compute_metrics([make_trade(4.0)] * 12 + [make_trade(-1.0)] * 28)
        report = analyse_from_metrics(metrics, 4.0, trials=200)
        assert report.win_rate < metrics.win_rate
        assert "lower bound" in report.win_rate_source

    def test_point_estimate_when_asked(self):
        metrics = compute_metrics([make_trade(4.0)] * 12 + [make_trade(-1.0)] * 28)
        report = analyse_from_metrics(metrics, 4.0, trials=200, use_lower_bound=False)
        assert report.win_rate == pytest.approx(metrics.win_rate)

    def test_small_sample_sizes_smaller_than_a_large_one(self):
        """Same observed rate, less evidence: the recommendation must shrink."""
        small = compute_metrics([make_trade(4.0)] * 3 + [make_trade(-1.0)] * 7)
        large = compute_metrics([make_trade(4.0)] * 90 + [make_trade(-1.0)] * 210)
        a = analyse_from_metrics(small, 4.0, trials=200)
        b = analyse_from_metrics(large, 4.0, trials=200)
        assert a.win_rate < b.win_rate
        assert a.recommended_risk < b.recommended_risk

    def test_empty_metrics_is_an_error(self):
        with pytest.raises(ValueError, match="zero trades"):
            analyse_from_metrics(compute_metrics([]), 4.0)


class TestMisestimation:
    def test_overestimating_the_edge_is_punished(self):
        """The asymmetry that justifies the lower-bound default."""
        grid = misestimation_grid([0.30, 0.40], [0.25], 4.0, trades=60, trials=600)
        by_assumed = {row[0]: row[2][0] for row in grid}
        assert by_assumed[0.40] < by_assumed[0.30]

    def test_correct_estimate_beats_an_overestimate(self):
        grid = misestimation_grid([0.25, 0.40], [0.25], 4.0, trades=60, trials=600)
        by_assumed = {row[0]: row[2][0] for row in grid}
        assert by_assumed[0.25] > by_assumed[0.40]

    def test_negative_edge_row_risks_nothing(self):
        grid = misestimation_grid([0.10], [0.30], 4.0, trades=30, trials=200)
        assert grid[0][1] == 0.0
