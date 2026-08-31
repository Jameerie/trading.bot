"""Forward predictions and the ledger that scores them.

The distinction these tests protect is the one the module exists for: a backtest
replays outcomes that were already in the file, and a prediction is a claim made
before the outcome existed. If a backtest could put an entry on the forward
scoreboard, the scoreboard would mean nothing, so that is asserted directly.

The other risk is subtler. A prediction that has not resolved yet must stay open.
Force-closing it — because the data ran out, or because the horizon looked long —
would quietly convert an unfinished claim into a scored one, and the scoring
would land wherever price happened to be.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.backtest import run_backtest
from trading_bot.clock import Clock
from trading_bot.config import Config
from trading_bot.data.synthetic import SyntheticSource
from trading_bot.forecast import (
    MIN_BASE_RATE_SAMPLE,
    bars_to_time,
    base_rate_from_metrics,
    build_prediction,
    empty_base_rate,
    format_prediction,
    measure_base_rate,
    resolve_open_predictions,
    scoreboard,
    settle,
)
from trading_bot.journal import Journal
from trading_bot.metrics import compute_metrics
from trading_bot.models import Direction, Outcome, Timeframe
from trading_bot.scanner import evaluate_at


def _first_signal(candles, symbol, config, start=250, stop=600):
    """Find a real signal with room after it, so settlement has bars to work on."""
    for i in range(start, min(stop, len(candles) - 1)):
        evaluation = evaluate_at(candles, i, symbol, config)
        if evaluation.has_signal:
            return i, evaluation.signal
    return None, None


@pytest.fixture
def gbpaud(config):
    return SyntheticSource().fetch("GBPAUD", Timeframe.H1, 800)


class TestHorizonArithmetic:
    def test_bars_advance_one_timeframe_at_a_time(self):
        start = datetime(2024, 5, 1, 8, 0, tzinfo=timezone.utc)  # Wednesday
        assert bars_to_time(start, 5, Timeframe.H1) == start + timedelta(hours=5)

    def test_the_weekend_does_not_count_as_market_time(self):
        """24 H1 bars from Friday evening lands on Monday, not Saturday.

        A deadline that ignores the shutdown puts a date in the user's calendar
        on which the market was never open.
        """
        friday = datetime(2024, 5, 3, 18, 0, tzinfo=timezone.utc)
        landed = bars_to_time(friday, 24, Timeframe.H1)
        assert landed.weekday() == 0, "Monday"
        assert landed > friday + timedelta(hours=24)

    def test_a_bigger_timeframe_reaches_further(self):
        start = datetime(2024, 5, 1, 8, 0, tzinfo=timezone.utc)
        assert bars_to_time(start, 4, Timeframe.H4) > bars_to_time(start, 4, Timeframe.H1)


class TestBaseRate:
    def test_a_thin_sample_is_reported_as_unmeasured(self):
        rate = empty_base_rate("EURUSD", "no history")
        assert not rate.is_measured
        assert "no measured base rate" in rate.headline
        assert "unscored" in rate.headline

    def test_a_real_sample_quotes_its_interval(self, config, gbpaud):
        rate = measure_base_rate(gbpaud, "GBPAUD", config)
        assert rate.sample >= 0
        if rate.is_measured:
            assert rate.interval.low <= rate.win_rate <= rate.interval.high
            assert "%" in rate.headline

    def test_the_source_window_travels_with_the_number(self, config, gbpaud):
        rate = measure_base_rate(gbpaud, "GBPAUD", config)
        assert "H1 bars" in rate.source

    def test_out_of_sample_is_labelled(self, config, gbpaud):
        rate = measure_base_rate(gbpaud, "GBPAUD", config, split=0.7)
        assert rate.out_of_sample
        assert "out-of-sample" in rate.source

    def test_too_little_history_measures_nothing(self, config, gbpaud):
        rate = measure_base_rate(gbpaud[:40], "GBPAUD", config)
        assert not rate.is_measured
        assert rate.sample == 0

    def test_the_sample_floor_is_enforced(self, config):
        """One win in one trade is not a base rate, whatever it looks like."""
        metrics = compute_metrics([], config.target.confidence)
        rate = base_rate_from_metrics("EURUSD", metrics, "nothing", False)
        assert not rate.is_measured
        assert MIN_BASE_RATE_SAMPLE > 1


class TestPrediction:
    def test_the_claim_is_falsifiable(self, config, gbpaud):
        _, signal = _first_signal(gbpaud, "GBPAUD", config)
        assert signal is not None
        prediction = build_prediction(signal, config)
        claim = prediction.claim
        assert str(signal.take_profit) in claim
        assert str(signal.stop_loss) in claim
        assert "before" in claim, "the claim must state which level comes first"

    def test_deadlines_come_from_the_measured_config(self, config, gbpaud):
        """Prediction windows must match what the base rate was measured under."""
        _, signal = _first_signal(gbpaud, "GBPAUD", config)
        prediction = build_prediction(signal, config)
        assert prediction.entry_window_bars == config.backtest.entry_expiry_bars
        assert prediction.horizon_bars == config.backtest.max_bars_in_trade
        assert prediction.entry_deadline < prediction.resolve_by

    def test_an_unmeasured_prediction_says_so_on_the_card(self, config, gbpaud):
        _, signal = _first_signal(gbpaud, "GBPAUD", config)
        prediction = build_prediction(signal, config)
        text = "\n".join(format_prediction(prediction, Clock("Africa/Lagos")))
        assert "THE PREDICTION" in text
        assert "unscored" in text or "%" in text
        assert "WAT" in text, "deadlines belong in the reader's own clock"


class TestSettlement:
    def test_a_resolved_prediction_is_scored_by_the_backtest_rules(self, config, gbpaud):
        index, signal = _first_signal(gbpaud, "GBPAUD", config)
        outcome = settle(signal, gbpaud, config)
        assert outcome.resolved
        assert outcome.outcome in (Outcome.WIN, Outcome.LOSS, Outcome.EXPIRED)
        assert outcome.exit_price is not None

    def test_an_unfinished_prediction_stays_open(self, config, gbpaud):
        """Running out of data is not the same as running out of time."""
        index, signal = _first_signal(gbpaud, "GBPAUD", config)
        outcome = settle(signal, gbpaud[: index + 3], config)
        assert not outcome.resolved
        assert "still open" in outcome.note
        assert outcome.r_multiple is None

    def test_settling_needs_the_bar_the_prediction_was_made_on(self, config, gbpaud):
        index, signal = _first_signal(gbpaud, "GBPAUD", config)
        outcome = settle(signal, gbpaud[index + 5 :], config)
        assert not outcome.resolved
        assert "not in the data" in outcome.note

    def test_no_bars_after_the_decision_bar_is_not_a_result(self, config, gbpaud):
        index, signal = _first_signal(gbpaud, "GBPAUD", config)
        outcome = settle(signal, gbpaud[: index + 1], config)
        assert not outcome.resolved

    def test_settlement_agrees_with_the_backtest(self, config, gbpaud):
        """The forward resolver and the simulator must not diverge.

        If they did, the base rate would be describing a different game from the
        one being scored.
        """
        index, signal = _first_signal(gbpaud, "GBPAUD", config)
        outcome = settle(signal, gbpaud, config)
        result = run_backtest(gbpaud, "GBPAUD", config, start=index, end=index)
        assert result.trades, "the backtest should take the same signal"
        trade = result.trades[0]
        assert trade.outcome is outcome.outcome
        assert trade.r_multiple == pytest.approx(outcome.r_multiple)


class TestScoreboard:
    def test_an_empty_journal_claims_nothing(self, tmp_path, config):
        board = scoreboard(Journal(tmp_path / "j.jsonl"))
        assert board.made == 0
        assert not board.has_verdict
        lines = board.summary(Clock())
        assert "No predictions on record yet" in lines[0]

    def test_a_backtest_contributes_nothing_to_the_forward_record(
        self, tmp_path, config, gbpaud
    ):
        """The whole point of the module, asserted directly."""
        journal = Journal(tmp_path / "j.jsonl")
        result = run_backtest(gbpaud, "GBPAUD", config)
        assert result.trades, "the backtest produced trades"
        assert scoreboard(journal).made == 0, "none of which reach the scoreboard"

    def test_made_predictions_appear_but_unresolved_ones_score_nothing(
        self, tmp_path, config, gbpaud
    ):
        journal = Journal(tmp_path / "j.jsonl")
        _, signal = _first_signal(gbpaud, "GBPAUD", config)
        journal.record(signal)
        board = scoreboard(journal)
        assert board.made == 1
        assert board.resolved == 0
        assert "no forward win rate" in " ".join(board.summary(Clock()))

    def test_resolving_moves_a_prediction_onto_the_board(self, tmp_path, config, gbpaud):
        journal = Journal(tmp_path / "j.jsonl")
        index, signal = _first_signal(gbpaud, "GBPAUD", config)
        journal.record(signal)

        class _Source:
            """Serves the full series, so the prediction can be settled."""

            def fetch(self, symbol, timeframe, bars):
                return gbpaud

        reports = resolve_open_predictions(journal, _Source(), config)
        assert reports and reports[0]["status"] == "resolved"

        board = scoreboard(journal)
        assert board.resolved == 1
        assert board.still_open == 0

    def test_a_symbol_with_no_data_is_left_open_not_guessed(self, tmp_path, config, gbpaud):
        journal = Journal(tmp_path / "j.jsonl")
        _, signal = _first_signal(gbpaud, "GBPAUD", config)
        journal.record(signal)

        class _Broken:
            def fetch(self, symbol, timeframe, bars):
                raise RuntimeError("provider down")

        reports = resolve_open_predictions(journal, _Broken(), config)
        assert reports[0]["status"] == "no data"
        assert scoreboard(journal).resolved == 0
