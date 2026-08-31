"""Currency exposure across concurrent signals.

Scanning three pairs, this was not a problem. Scanning sixty, it is the main one:
long EURUSD, long EURJPY and short EURGBP is a single euro position at three
times the intended size, and a list of three cards looks like diversification.

Two rules are pinned here, and both matter:

* the netting arithmetic must be right, because everything else rests on it;
* nothing in this module may *act*. It ranks, it warns, it suggests a subset —
  and the signals it left out are still returned to the caller, still printed,
  still the user's to take. A module that could silently drop a signal would be
  the software deciding.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot import exposure
from trading_bot.config import AccountConfig, Config
from trading_bot.forecast import BaseRate
from trading_bot.metrics import Interval
from trading_bot.models import Direction, Signal, Timeframe

ISSUED = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)


def make_signal(symbol: str, direction: Direction, rr: float = 5.0, score: float = 90.0):
    return Signal(
        symbol=symbol,
        timeframe=Timeframe.H1,
        direction=direction,
        entry=1.1000,
        stop_loss=1.0980,
        take_profit=1.1100,
        issued_at=ISSUED,
        score=score,
        max_score=122.0,
        risk_reward=rr,
        risk_pips=20.0,
        reward_pips=rr * 20.0,
        position_lots=0.5,
        risk_amount=100.0,
    )


def base_rate(symbol: str, sample: int, low: float, win: float = 0.35):
    return BaseRate(
        symbol=symbol,
        sample=sample,
        win_rate=win,
        interval=Interval(low, min(low + 0.3, 1.0), 0.95),
        expectancy_r=0.5,
        average_rr=5.0,
        source="test",
        out_of_sample=True,
    )


class TestNetting:
    def test_a_long_pair_is_long_the_base_and_short_the_quote(self, config):
        rows = {e.code: e for e in exposure.compute_exposure(
            [make_signal("EURUSD", Direction.LONG)], config)}
        assert rows["EUR"].net_risk_pct == pytest.approx(config.account.risk_per_trade_pct)
        assert rows["USD"].net_risk_pct == pytest.approx(-config.account.risk_per_trade_pct)

    def test_three_euro_longs_are_one_euro_bet_at_triple_size(self, config):
        signals = [
            make_signal("EURUSD", Direction.LONG),
            make_signal("EURJPY", Direction.LONG),
            make_signal("EURGBP", Direction.LONG),
        ]
        rows = {e.code: e for e in exposure.compute_exposure(signals, config)}
        assert rows["EUR"].net_risk_pct == pytest.approx(3 * config.account.risk_per_trade_pct)
        assert len(rows["EUR"].legs) == 3

    def test_opposing_legs_net_to_flat_and_are_called_out(self, config):
        """Long EURUSD and long USDCHF partly cancel on the dollar."""
        signals = [
            make_signal("EURUSD", Direction.LONG),
            make_signal("USDCHF", Direction.LONG),
        ]
        rows = {e.code: e for e in exposure.compute_exposure(signals, config)}
        assert rows["USD"].direction == "flat"
        assert rows["USD"].is_netted
        assert "cancel" in rows["USD"].describe()

    def test_short_is_the_mirror_of_long(self, config):
        long_rows = {e.code: e for e in exposure.compute_exposure(
            [make_signal("GBPJPY", Direction.LONG)], config)}
        short_rows = {e.code: e for e in exposure.compute_exposure(
            [make_signal("GBPJPY", Direction.SHORT)], config)}
        assert long_rows["GBP"].net_risk_pct == -short_rows["GBP"].net_risk_pct


class TestWarnings:
    def test_exceeding_the_concurrent_budget_is_flagged(self, config):
        signals = [make_signal(s, Direction.LONG)
                   for s in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD")]
        report = exposure.analyse(signals, config)
        assert report.over_budget
        assert any("above the" in w for w in report.warnings)

    def test_a_concentrated_currency_is_named_as_one_bet(self, config):
        signals = [make_signal(s, Direction.LONG)
                   for s in ("EURUSD", "EURJPY", "EURGBP", "EURAUD")]
        report = exposure.analyse(signals, config)
        assert any("one bet, not" in w for w in report.warnings)
        assert report.concentrated

    def test_a_single_signal_raises_nothing(self, config):
        report = exposure.analyse([make_signal("EURUSD", Direction.LONG)], config)
        assert not report.over_budget
        assert report.warnings == ()


class TestSubsetChoice:
    def test_the_budget_caps_how_many_are_suggested(self, config):
        signals = [make_signal(s, Direction.LONG)
                   for s in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD")]
        rates = {s.symbol: base_rate(s.symbol, 40, 0.3) for s in signals}
        report = exposure.analyse(signals, config, rates)
        budget = config.account.max_concurrent_risk_pct / config.account.risk_per_trade_pct
        assert len(report.suggested) <= budget

    def test_nothing_is_ever_removed_from_the_caller_s_signals(self, config):
        """The suggestion is advice. The signals themselves are untouched."""
        signals = [make_signal(s, Direction.LONG)
                   for s in ("EURUSD", "EURJPY", "EURGBP", "EURAUD", "EURCAD")]
        report = exposure.analyse(signals, config)
        assert report.signal_count == 5
        assert len(report.suggested) + len(report.dropped) == 5

    def test_every_signal_left_out_is_given_a_reason(self, config):
        signals = [make_signal(s, Direction.LONG)
                   for s in ("EURUSD", "EURJPY", "EURGBP", "EURAUD")]
        report = exposure.analyse(signals, config)
        for symbol, reason in report.dropped:
            assert reason, f"{symbol} was dropped without a reason"

    def test_ranking_reads_the_lower_bound_not_the_point_estimate(self, config):
        """A 40% win rate on nine trades must not outrank 30% on ninety.

        This is the project's sizing rule applied to selection, and it bites
        harder here: the ranking decides which trades get taken at all.
        """
        flattering = make_signal("EURUSD", Direction.LONG)
        solid = make_signal("AUDCAD", Direction.LONG)
        rates = {
            "EURUSD": base_rate("EURUSD", 9, low=0.10, win=0.44),
            "AUDCAD": base_rate("AUDCAD", 90, low=0.26, win=0.30),
        }
        ranked = exposure.choose_subset([flattering, solid], config, rates)[0]
        assert ranked[0].symbol == "AUDCAD"

    def test_an_unmeasured_signal_ranks_below_every_measured_one(self, config):
        measured = make_signal("EURUSD", Direction.LONG)
        unmeasured = make_signal("AUDCAD", Direction.LONG)
        rates = {"EURUSD": base_rate("EURUSD", 40, low=0.25)}
        ranked = exposure.choose_subset([unmeasured, measured], config, rates)[0]
        assert ranked[0].symbol == "EURUSD"
        assert not ranked[-1].measured

    def test_an_unmeasured_signal_claims_no_expectancy(self, config):
        ranked = exposure.expected_r(make_signal("EURUSD", Direction.LONG), None)
        assert not ranked.measured
        assert "no measured base rate" in ranked.basis


class TestRendering:
    def test_the_block_says_it_decides_nothing(self, config):
        signals = [make_signal(s, Direction.LONG) for s in ("EURUSD", "EURJPY")]
        text = exposure.format_exposure(exposure.analyse(signals, config), config)
        assert "not a decision" in text
        assert "cancels a signal" in text

    def test_no_signals_renders_nothing(self, config):
        assert exposure.format_exposure(exposure.analyse([], config), config) == ""
