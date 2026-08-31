"""Edge over a random walk.

Win rate alone cannot be compared between strategies, because the ratio that
produced it changes what the number means: 45% at 1.5:1 is a *smaller* edge than
25% at 4:1. This module tests the measure that makes them comparable, and — more
importantly — tests the two ways it could be made to lie.
"""

from __future__ import annotations

import dataclasses

import pytest

from trading_bot.metrics import (
    Metrics,
    compute_metrics,
    effective_ratio,
    measure_edge,
    random_baseline,
    wilson_interval,
)
from trading_bot.models import Outcome

from test_backtest import make_signal
from test_metrics import make_trade


def metrics_for(wins: int, total: int, planned_rr: float, *, avg_win: float | None = None,
                expired: int = 0) -> Metrics:
    """A Metrics object with just the fields the edge measure reads."""
    losses = total - wins
    win_r = avg_win if avg_win is not None else planned_rr
    return Metrics(
        trades=total, wins=wins, losses=losses, breakeven=0, expired=expired,
        win_rate=wins / total, win_rate_interval=wilson_interval(wins, total, 0.95),
        expectancy_r=0.0, total_r=0.0, profit_factor=0.0,
        average_win_r=win_r, average_loss_r=-1.0, max_drawdown_r=0.0,
        max_win_streak=0, max_loss_streak=0, average_bars_held=0.0,
        average_rr_planned=planned_rr, average_mae_r=0.0, average_mfe_r=0.0,
    )


class TestRandomBaseline:
    @pytest.mark.parametrize("rr,expected", [(4.0, 0.20), (1.5, 0.40), (1.0, 0.50), (9.0, 0.10)])
    def test_known_ratios(self, rr, expected):
        """A driftless walk reaches a target R times as far away 1/(1+R) of the time."""
        assert random_baseline(rr) == pytest.approx(expected)

    def test_our_floor_is_a_one_in_five_coin(self):
        """The 1:4 product floor means chance alone wins 20% of the time."""
        assert random_baseline(4.0) == pytest.approx(0.20)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_impossible_ratios(self, bad):
        with pytest.raises(ValueError):
            random_baseline(bad)


class TestEffectiveRatio:
    def test_uses_the_planned_ratio_when_trades_reach_their_target(self):
        assert effective_ratio(metrics_for(20, 50, 4.0, avg_win=4.0)) == pytest.approx(4.0)

    def test_prefers_the_realised_ratio_when_it_is_worse(self):
        """Planning 10:1 and collecting 2:1 is a 2:1 strategy, and is scored as one."""
        assert effective_ratio(metrics_for(20, 50, 10.0, avg_win=2.0)) == pytest.approx(2.0)

    def test_never_credits_a_target_that_was_not_reached(self):
        """The regression that matters.

        Scoring a 10:1 plan against a 10:1 baseline sets a bar of 9%, which almost
        anything clears. If trades actually pay 2:1 the honest bar is 33%. Taking
        the *harder* of the two is what stops a distant, rarely-filled target from
        manufacturing an edge that is not there.
        """
        reached = metrics_for(20, 50, 10.0, avg_win=10.0)
        assert measure_edge(reached).verdict == "EDGE CONFIRMED"

        # Identical win rate and identical plan, but the target was never reached.
        missed = metrics_for(20, 50, 10.0, avg_win=2.0)
        assert missed.win_rate == reached.win_rate
        assert random_baseline(effective_ratio(missed)) > random_baseline(10.0)
        assert measure_edge(missed).verdict != "EDGE CONFIRMED"

    def test_falls_back_to_planned_without_both_outcomes(self):
        """With no losses there is no realised ratio to compute."""
        assert effective_ratio(metrics_for(50, 50, 4.0)) == pytest.approx(4.0)


class TestVerdicts:
    def test_no_trades_says_so(self):
        assert measure_edge(compute_metrics([])).verdict == "NO DATA"

    def test_small_sample_claims_nothing(self):
        """Mirrors the quality gate: a lucky ten trades proves nothing."""
        edge = measure_edge(metrics_for(8, 10, 4.0), min_sample=30)
        assert edge.verdict == "INSUFFICIENT DATA"
        assert not edge.proven
        assert "below the 30-trade minimum" in edge.detail

    def test_confirms_a_clear_edge_on_a_large_sample(self):
        edge = measure_edge(metrics_for(400, 1000, 4.0))
        assert edge.verdict == "EDGE CONFIRMED"
        assert edge.proven
        assert edge.edge == pytest.approx(0.20, abs=0.01)

    def test_a_result_below_chance_has_no_edge(self):
        edge = measure_edge(metrics_for(10, 100, 4.0))
        assert edge.verdict == "NO EDGE"
        assert edge.edge < 0

    def test_decided_on_the_lower_bound_not_the_point_estimate(self):
        """The whole discipline of this project in one assertion.

        Two results with the *same* 25% win rate at the same 4:1 ratio, both above
        the 20% chance baseline by the point estimate. Only the large sample is
        confirmed; the small one stays unproven, because 40 trades cannot rule out
        a coin flip. Nothing but the evidence separates them.
        """
        small = measure_edge(metrics_for(10, 40, 4.0), min_sample=30)
        large = measure_edge(metrics_for(250, 1000, 4.0), min_sample=30)
        assert small.win_rate == large.win_rate == pytest.approx(0.25)
        assert small.edge == large.edge == pytest.approx(0.05)
        assert small.verdict == "UNPROVEN"
        assert small.lower_bound_edge < 0
        assert large.verdict == "EDGE CONFIRMED"
        assert large.lower_bound_edge > 0


class TestPublishedClaims:
    def test_a_published_45_percent_at_1_5_to_1_is_unproven(self):
        """Regression fixture: FvgGold-EA, an open-source EA with our architecture.

        It publishes 29 wins from 64 trades at 1.5:1 — 45.3%, which reads well
        until it is set against the 40% a coin flip gives at that ratio. The
        interval reaches below chance, so the honest verdict is that the sample
        cannot tell the two apart. Kept as a test because this is the exact shape
        of claim the project must never make about itself.
        """
        edge = measure_edge(metrics_for(29, 64, 1.5, avg_win=1.5))
        assert edge.baseline == pytest.approx(0.40)
        assert edge.edge == pytest.approx(0.053, abs=0.002)
        assert edge.lower_bound_edge < 0
        assert edge.verdict == "UNPROVEN"

    def test_a_lower_win_rate_at_a_higher_ratio_is_the_bigger_edge(self):
        """25% at 4:1 beats 45% at 1.5:1, though it looks far worse."""
        modest = measure_edge(metrics_for(300, 1000, 4.0, avg_win=4.0))
        flashy = measure_edge(metrics_for(453, 1000, 1.5, avg_win=1.5))
        assert flashy.win_rate > modest.win_rate
        assert modest.edge > flashy.edge


class TestThroughRealTrades:
    def test_expiries_are_flagged_when_they_dominate(self):
        """Trades closing on the clock break the two-barrier model this rests on."""
        signal = dataclasses.replace(make_signal(), risk_reward=4.0)
        trades = [
            dataclasses.replace(make_trade(4.0), signal=signal) for _ in range(20)
        ] + [
            dataclasses.replace(make_trade(-1.0, Outcome.EXPIRED), signal=signal)
            for _ in range(20)
        ]
        edge = measure_edge(compute_metrics(trades))
        assert "time limit" in edge.detail

    def test_matches_a_hand_computed_baseline(self):
        signal = dataclasses.replace(make_signal(), risk_reward=4.0)
        trades = [dataclasses.replace(make_trade(4.0), signal=signal) for _ in range(15)]
        trades += [dataclasses.replace(make_trade(-1.0), signal=signal) for _ in range(35)]
        edge = measure_edge(compute_metrics(trades))
        assert edge.risk_reward == pytest.approx(4.0)
        assert edge.baseline == pytest.approx(0.20)
        assert edge.win_rate == pytest.approx(0.30)
        assert edge.edge == pytest.approx(0.10)
