"""The prediction ledger: every call, what the model saw, and what happened.

Two things are protected here above all. A replay must reproduce the
backtester's trades exactly and must never touch the journal, because the
moment a replayed outcome can reach the forward record the record means
nothing. And a settled prediction must carry what the market actually did — the
fill, the excursions, the bars from fill to exit — measured by the simulator's
own rule, so that "what happened" is a record and not a recollection.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from trading_bot.backtest import run_backtest
from trading_bot.config import Config
from trading_bot.data.synthetic import SyntheticSource
from trading_bot.forecast import build_prediction, resolve_open_predictions, scoreboard, settle
from trading_bot.journal import Journal
from trading_bot.ledger import (
    ORIGIN_FORWARD,
    ORIGIN_REPLAY,
    STATE_ENTRY_OPEN,
    STATE_NO_DATA,
    STATE_RESOLVED,
    STATE_RUNNING,
    STATE_WAITING,
    breakdown,
    by_grade,
    by_symbol,
    calibration,
    case_from_entry,
    check_attribution,
    confidence_band,
    describe_result,
    format_case,
    format_case_line,
    format_live,
    format_scorecards,
    format_table,
    live_status,
    load_cases,
    outcome_detail,
    replay,
    snapshot,
    sparkline,
    summarise,
)
from trading_bot.models import Direction, Outcome, Timeframe
from trading_bot.scanner import evaluate_at
from trading_bot.strategy.trend_pullback import DEFAULT_CHECKS


@pytest.fixture
def gbpaud():
    return SyntheticSource().fetch("GBPAUD", Timeframe.H1, 1400)


def _signals(candles, symbol, config, want=4, start=250):
    """A handful of real signals with room after them, as (index, evaluation)."""
    found = []
    i = start
    while i < len(candles) - 260 and len(found) < want:
        evaluation = evaluate_at(candles, i, symbol, config)
        if evaluation.has_signal:
            found.append((i, evaluation))
            i += 260  # leave the horizon clear so the next one is independent
        else:
            i += 1
    return found


class _Source:
    """Serves a fixed series, so predictions can be settled against it."""

    def __init__(self, candles):
        self.candles = candles

    def fetch(self, symbol, timeframe, bars):
        return self.candles


@pytest.fixture
def journal_with_outcomes(tmp_path, config, gbpaud):
    """Predictions journalled with their snapshots, then settled against the full series."""
    journal = Journal(tmp_path / "j.jsonl")
    found = _signals(gbpaud, "GBPAUD", config)
    assert len(found) >= 2, "the fixture needs at least two signals"
    for _, evaluation in found:
        prediction = build_prediction(evaluation.signal, config)
        journal.record(evaluation.signal, context=snapshot(evaluation, config, prediction))
    resolve_open_predictions(journal, _Source(gbpaud), config)
    return journal


class TestReplay:
    def test_replay_matches_the_backtest_trade_for_trade(self, config, gbpaud):
        cases = replay(gbpaud, "GBPAUD", config)
        result = run_backtest(gbpaud, "GBPAUD", config)
        assert cases, "the series should produce calls"
        assert len(cases) == len(result.trades)
        for case, trade in zip(cases, result.trades):
            assert case.result.outcome is trade.outcome
            assert case.result.r_multiple == pytest.approx(trade.r_multiple)
            assert case.result.bars_held == trade.bars_held
            assert case.made_at == trade.signal.issued_at

    def test_a_replay_never_touches_the_journal(self, tmp_path, config, gbpaud):
        """The whole point of the two ledgers, asserted directly."""
        journal = Journal(tmp_path / "j.jsonl")
        cases = replay(gbpaud, "GBPAUD", config)
        assert cases
        assert not journal.path.exists()
        assert scoreboard(journal).made == 0

    def test_replayed_cases_are_labelled_on_every_case(self, config, gbpaud):
        for case in replay(gbpaud, "GBPAUD", config):
            assert case.origin == ORIGIN_REPLAY
            assert "outcome was in the file" in case.result.note
            assert "REPLAY" in "\n".join(format_case(case, config.clock, config))

    def test_the_path_runs_from_the_fill_bar_to_the_exit_bar(self, config, gbpaud):
        case = replay(gbpaud, "GBPAUD", config)[0]
        path = case.result.path
        assert len(path) == case.result.bars_held + 1
        assert path[0].timestamp == case.result.fill_time
        assert path[-1].timestamp == case.result.exit_time

    def test_a_split_replays_only_the_tail(self, config, gbpaud):
        boundary = int(len(gbpaud) * 0.7)
        tail = replay(gbpaud, "GBPAUD", config, start=boundary)
        assert all(gbpaud.index(next(c for c in gbpaud if c.timestamp == case.made_at)) >= boundary
                   for case in tail)


class TestSnapshot:
    def test_every_check_is_on_the_record_fired_or_not(self, config, gbpaud):
        _, evaluation = _signals(gbpaud, "GBPAUD", config, want=1)[0]
        snap = snapshot(evaluation, config)
        codes = [row["code"] for row in snap["checks"]]
        assert codes == [check.code for check in DEFAULT_CHECKS]
        fired = {row["code"] for row in snap["checks"] if row["fired"]}
        assert fired == {r.code for r in evaluation.signal.reasons}
        assert any(not row["fired"] for row in snap["checks"]) or len(fired) == len(codes)
        assert snap["readings"]["price"] == pytest.approx(evaluation.context.price)
        assert snap["session"]

    def test_the_snapshot_round_trips_through_the_journal(self, tmp_path, config, gbpaud):
        journal = Journal(tmp_path / "j.jsonl")
        _, evaluation = _signals(gbpaud, "GBPAUD", config, want=1)[0]
        prediction = build_prediction(evaluation.signal, config)
        journal.record(evaluation.signal, context=snapshot(evaluation, config, prediction))

        case = load_cases(journal, config)[0]
        assert case.snapshot_complete
        assert [c.code for c in case.checks] == [check.code for check in DEFAULT_CHECKS]
        assert {c.code for c in case.fired} == {r.code for r in evaluation.signal.reasons}
        assert case.readings["adx"] == pytest.approx(evaluation.context.adx, abs=0.01)
        assert case.prediction.resolve_by == prediction.resolve_by

    def test_deadlines_are_read_back_not_recomputed(self, tmp_path, config, gbpaud):
        """Changing the horizon later must not move the goalposts on a recorded claim."""
        journal = Journal(tmp_path / "j.jsonl")
        _, evaluation = _signals(gbpaud, "GBPAUD", config, want=1)[0]
        prediction = build_prediction(evaluation.signal, config)
        journal.record(evaluation.signal, context=snapshot(evaluation, config, prediction))

        shorter = replace(config, backtest=replace(config.backtest, max_bars_in_trade=50))
        case = load_cases(journal, shorter)[0]
        assert case.prediction.horizon_bars == config.backtest.max_bars_in_trade
        assert case.prediction.resolve_by == prediction.resolve_by

    def test_an_entry_from_before_the_ledger_is_reconstructed_and_flagged(
        self, tmp_path, config, gbpaud
    ):
        journal = Journal(tmp_path / "j.jsonl")
        _, evaluation = _signals(gbpaud, "GBPAUD", config, want=1)[0]
        journal.record(evaluation.signal)  # no snapshot, as older versions wrote it

        case = load_cases(journal, config)[0]
        assert not case.snapshot_complete
        assert len(case.checks) == len(DEFAULT_CHECKS)
        assert {c.code for c in case.fired} == {r.code for r in evaluation.signal.reasons}
        text = "\n".join(format_case(case, config.clock, config))
        assert "inferred" in text


class TestSettlement:
    def test_resolution_stores_what_happened(self, journal_with_outcomes, config, gbpaud):
        cases = load_cases(journal_with_outcomes, config)
        resolved = [c for c in cases if c.is_resolved]
        assert resolved, "the fixture settles at least one prediction"
        for case in resolved:
            r = case.result
            assert r.fill_price is not None
            assert r.fill_time is not None
            assert r.bars_held is not None
            assert r.path, "the bars from fill to exit travel with the close"
            assert r.path[0].timestamp == r.fill_time
            assert r.r_basis == "fill with costs"
            assert case.origin == ORIGIN_FORWARD

    def test_the_recorded_r_is_the_simulators_not_the_plans(
        self, journal_with_outcomes, config, gbpaud
    ):
        """The forward record is scored by the rule that produced the base rate."""
        for case in load_cases(journal_with_outcomes, config):
            if case.is_open:
                continue
            settled = settle(case.signal, gbpaud, config)
            assert settled.resolved
            assert case.result.r_multiple == pytest.approx(settled.r_multiple, abs=1e-4)
            assert case.result.outcome is settled.outcome

    def test_a_hand_closed_trade_is_labelled_as_such(self, tmp_path, config, gbpaud):
        journal = Journal(tmp_path / "j.jsonl")
        _, evaluation = _signals(gbpaud, "GBPAUD", config, want=1)[0]
        journal.record(evaluation.signal, context=snapshot(evaluation, config))
        journal.close(journal.read()[0].entry_id, exit_price=evaluation.signal.take_profit)

        case = load_cases(journal, config)[0]
        assert case.result.r_basis == "planned entry"
        assert case.result.path == ()
        assert "closed by hand" in "\n".join(format_case(case, config.clock, config))

    def test_outcome_detail_caps_the_path(self, config, gbpaud):
        result = run_backtest(gbpaud, "GBPAUD", config)
        trade = result.trades[0]
        index = next(i for i, c in enumerate(gbpaud) if c.timestamp == trade.signal.issued_at)
        detail = outcome_detail(trade, gbpaud, index + 1)
        assert len(detail["path"]) == trade.bars_held + 1
        assert detail["path"][0][0] == int(trade.entry_time.timestamp())
        assert detail["r_multiple"] == pytest.approx(trade.r_multiple, abs=1e-4)

    def test_the_ledger_and_the_scoreboard_agree(self, journal_with_outcomes, config):
        board = scoreboard(journal_with_outcomes, config.target.confidence)
        summary = summarise(load_cases(journal_with_outcomes, config), config)
        assert summary.made == board.made
        assert summary.resolved == board.resolved
        assert summary.metrics.win_rate == pytest.approx(board.metrics.win_rate)


class TestScorecards:
    def test_summary_counts_only_resolved_cases(self, tmp_path, config, gbpaud):
        journal = Journal(tmp_path / "j.jsonl")
        for _, evaluation in _signals(gbpaud, "GBPAUD", config, want=2):
            journal.record(evaluation.signal, context=snapshot(evaluation, config))
        summary = summarise(load_cases(journal, config), config)
        assert summary.made == 2
        assert summary.resolved == 0
        assert summary.still_open == 2
        assert summary.metrics.is_empty
        assert "no win rate" in " ".join(summary.lines(config.clock))

    def test_cash_is_r_times_the_risk_on_the_card(self, config, gbpaud):
        case = replay(gbpaud, "GBPAUD", config)[0]
        assert case.cash == pytest.approx(case.result.r_multiple * case.signal.risk_amount, abs=0.01)

    def test_breakdown_orders_by_sample_size_never_by_win_rate(self, config, gbpaud):
        cases = replay(gbpaud, "GBPAUD", config)
        buckets = breakdown(cases, by_grade, config.target.confidence)
        sizes = [b.trades for b in buckets]
        assert sizes == sorted(sizes, reverse=True)
        assert sum(sizes) == len(cases)

    def test_an_explicit_order_is_honoured(self, config, gbpaud):
        cases = replay(gbpaud, "GBPAUD", config)
        bands = calibration(cases, config.target.confidence)
        labels = [b.label for b in bands]
        order = ["below 70%", "70-74%", "75-79%", "80-84%", "85%+"]
        assert labels == [label for label in order if label in labels]

    @pytest.mark.parametrize(
        "value,label",
        [(0.5, "below 70%"), (0.70, "70-74%"), (0.749, "70-74%"), (0.75, "75-79%"),
         (0.84, "80-84%"), (0.85, "85%+"), (1.0, "85%+")],
    )
    def test_confidence_bands(self, value, label):
        assert confidence_band(value) == label

    def test_every_bucket_carries_its_interval(self, config, gbpaud):
        cases = replay(gbpaud, "GBPAUD", config)
        for b in breakdown(cases, by_symbol, config.target.confidence):
            assert 0.0 <= b.interval.low <= b.win_rate <= b.interval.high <= 1.0
            assert b.wins + b.losses <= b.trades

    def test_check_attribution_splits_every_case_between_fired_and_missing(self, config, gbpaud):
        cases = replay(gbpaud, "GBPAUD", config)
        effects = check_attribution(cases, config.target.confidence)
        assert {e.code for e in effects} == {check.code for check in DEFAULT_CHECKS}
        for effect in effects:
            assert effect.fired.trades + effect.missing.trades == len(cases)
            if effect.fired.trades and effect.missing.trades:
                assert effect.difference == pytest.approx(
                    effect.fired.win_rate - effect.missing.win_rate
                )
            else:
                assert effect.difference is None

    def test_scorecards_refuse_a_verdict_below_the_minimum_sample(self, config, gbpaud):
        cases = replay(gbpaud, "GBPAUD", config)
        text = "\n".join(format_scorecards(cases, config))
        assert "n beside" not in text  # the heading is the caller's; the tables carry n
        if len(cases) < config.target.min_sample:
            assert f"below the {config.target.min_sample}" in text
        assert "BY CHECK" in text
        assert "does the number mean anything" in text


class TestLiveStatus:
    def _case(self, config, gbpaud):
        index, evaluation = _signals(gbpaud, "GBPAUD", config, want=1)[0]
        journal_free = replay  # noqa: F841 - readability: the case is built from the evaluation
        prediction = build_prediction(evaluation.signal, config)
        entry = Journal.__new__(Journal)  # not used; build the case directly instead
        del entry
        from trading_bot.journal import JournalEntry
        from trading_bot.models import utc_now

        entry = JournalEntry(
            recorded_at=utc_now(), signal=evaluation.signal.to_dict(),
            context=snapshot(evaluation, config, prediction),
        )
        return index, case_from_entry(entry, config)

    def test_no_bar_yet_and_inside_the_window_is_an_open_entry(self, config, gbpaud):
        index, case = self._case(config, gbpaud)
        now = case.made_at + timedelta(minutes=30)
        status = live_status(case, gbpaud[: index + 1], config, now=now)
        assert status.state == STATE_ENTRY_OPEN
        assert status.bars_since == 0
        assert any("Limit" in line for line in status.advice)

    def test_no_bar_yet_but_the_window_has_passed_means_stale_data(self, config, gbpaud):
        index, case = self._case(config, gbpaud)
        now = case.made_at + timedelta(days=30)
        status = live_status(case, gbpaud[: index + 1], config, now=now)
        assert status.state == STATE_WAITING
        assert any("stale" in line for line in status.advice)

    def test_running_advice_is_conditional_on_whether_you_are_in(self, config, gbpaud):
        index, case = self._case(config, gbpaud)
        # Three bars closed, well past the entry deadline: whatever happened, the
        # tool cannot know whether the human placed it, so it says both things.
        window = gbpaud[: index + 4]
        now = case.made_at + timedelta(days=2)
        status = live_status(case, window, config, now=now)
        if status.state == STATE_RUNNING:
            text = " ".join(status.advice)
            assert "If you are NOT in" in text
            assert "If you ARE in" in text
            assert status.to_target_pips is not None and status.to_stop_pips is not None
            assert status.unrealised_r is not None
        else:
            assert status.state == STATE_RESOLVED

    def test_the_whole_series_resolves_it(self, config, gbpaud):
        index, case = self._case(config, gbpaud)
        status = live_status(case, gbpaud, config, now=case.made_at + timedelta(days=60))
        assert status.state == STATE_RESOLVED
        assert any("ledger --resolve" in line for line in status.advice)

    def test_missing_decision_bar_is_no_data(self, config, gbpaud):
        index, case = self._case(config, gbpaud)
        status = live_status(case, gbpaud[index + 5 :], config)
        assert status.state == STATE_NO_DATA

    def test_format_live_prints_the_state_and_the_advice(self, config, gbpaud):
        index, case = self._case(config, gbpaud)
        status = live_status(case, gbpaud[: index + 1], config, now=case.made_at)
        text = "\n".join(format_live(status, config.clock, config))
        assert STATE_ENTRY_OPEN in text
        assert "GBPAUD" in text

    def test_status_serialises(self, config, gbpaud):
        index, case = self._case(config, gbpaud)
        status = live_status(case, gbpaud, config, now=case.made_at)
        payload = status.to_dict(config.clock)
        json.dumps(payload)
        assert payload["state"] == status.state
        assert "resolve_by_local" in payload


class TestRendering:
    def test_a_case_file_has_every_section(self, config, gbpaud):
        case = replay(gbpaud, "GBPAUD", config)[0]
        text = "\n".join(format_case(case, config.clock, config, number=1))
        for heading in ("THE CALL", "WHAT THE MODEL SAW", "WHAT HAPPENED", "Readings:"):
            assert heading in text
        assert "  + " in text, "fired checks are marked"
        assert "not met:" in text or all(c.fired for c in case.checks)
        assert "closes, fill to exit" in text
        assert case.result.verdict in text

    def test_an_open_forward_case_says_nothing_happened_yet(self, tmp_path, config, gbpaud):
        journal = Journal(tmp_path / "j.jsonl")
        _, evaluation = _signals(gbpaud, "GBPAUD", config, want=1)[0]
        journal.record(evaluation.signal, context=snapshot(evaluation, config))
        case = load_cases(journal, config)[0]
        text = "\n".join(format_case(case, config.clock, config))
        assert "PREDICTION made" in text
        assert "Nothing yet" in text
        assert "OPEN" in format_case_line(case, config.clock, 1)

    def test_the_narrative_uses_plain_words(self, config, gbpaud):
        from trading_bot.instruments import get_instrument

        instrument = get_instrument("GBPAUD")
        for case in replay(gbpaud, "GBPAUD", config):
            words = describe_result(case, instrument, config.clock)
            if case.result.outcome is Outcome.WIN:
                assert words.startswith("RIGHT")
            elif case.result.outcome is Outcome.LOSS:
                assert words.startswith("WRONG")
            elif case.result.outcome is Outcome.EXPIRED:
                assert words.startswith("EXPIRED")
            assert "R =" in words

    def test_the_table_is_newest_first(self, config, gbpaud):
        cases = replay(gbpaud, "GBPAUD", config)
        lines = format_table(cases, config.clock)
        assert lines[2].strip().startswith(f"#{len(cases)}")

    def test_sparkline_fits_the_width(self, config, gbpaud):
        case = max(replay(gbpaud, "GBPAUD", config), key=lambda c: len(c.result.path))
        assert len(sparkline(case.result.path, width=40)) <= 40
        assert sparkline(()) == ""

    def test_case_files_serialise_with_their_origin(self, config, gbpaud):
        cases = replay(gbpaud, "GBPAUD", config)
        payload = [c.to_dict(config.clock) for c in cases]
        json.dumps(payload)
        assert all(item["origin"] == ORIGIN_REPLAY for item in payload)
        assert all(len(item["checks"]) == len(DEFAULT_CHECKS) for item in payload)
        assert all("narrative" in item["result"] for item in payload)
        assert all(item["result"]["path"] for item in payload)
