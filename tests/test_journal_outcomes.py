"""Journal outcome recording.

The journal is the only place live results enter the system, so the tests here
care about two things above all: a record cannot be silently altered, and a
trade cannot be counted twice.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.errors import DataError
from trading_bot.journal import (
    Journal,
    classify_outcome,
    realised_r,
    signal_id,
)
from trading_bot.models import Direction, Outcome

from test_backtest import make_signal


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "j.jsonl")


class TestRealisedR:
    def test_target_hit_on_a_long(self):
        # entry 1.1000, stop 1.0980 -> 20 pips of risk; exit 1.1085 -> 85 pips.
        signal = make_signal().to_dict()
        assert realised_r(signal, 1.1085) == pytest.approx(4.25)

    def test_stop_hit_is_minus_one(self):
        assert realised_r(make_signal().to_dict(), 1.0980) == pytest.approx(-1.0)

    def test_partial_exit_is_reported_as_partial(self):
        """Closing early at half the target is +2R, not a full win."""
        assert realised_r(make_signal().to_dict(), 1.1040) == pytest.approx(2.0)

    def test_short_direction(self):
        signal = replace(
            make_signal(), direction=Direction.SHORT, entry=1.1000,
            stop_loss=1.1020, take_profit=1.0915,
        ).to_dict()
        assert realised_r(signal, 1.0915) == pytest.approx(4.25)
        assert realised_r(signal, 1.1020) == pytest.approx(-1.0)

    def test_broken_signal_is_rejected(self):
        bad = make_signal().to_dict()
        bad["stop_loss"] = bad["entry"] + 0.001  # stop on the wrong side
        with pytest.raises(DataError, match="wrong side"):
            realised_r(bad, 1.11)

    @pytest.mark.parametrize(
        "value,expected",
        [(4.0, Outcome.WIN), (-1.0, Outcome.LOSS), (0.0, Outcome.BREAKEVEN)],
    )
    def test_classification(self, value, expected):
        assert classify_outcome(value) is expected


class TestSnapshots:
    def test_context_travels_with_the_signal(self, journal):
        context = {"checks": [{"code": "ADX", "weight": 8.0, "fired": True, "detail": "ADX 31"}],
                   "readings": {"adx": 31.2}}
        journal.record(make_signal(), context=context)
        entry = journal.read()[0]
        assert entry.context == context
        assert entry.detail == {}

    def test_an_entry_without_context_reads_as_empty(self, journal):
        journal.record(make_signal())
        assert journal.read()[0].context == {}

    def test_a_close_can_carry_the_simulators_r_and_detail(self, journal):
        entry = journal.record(make_signal())
        detail = {"fill_price": 1.1001, "bars_held": 7, "path": [[1704067200, 1.1, 1.11, 1.09, 1.105]]}
        journal.close(entry.entry_id, exit_price=1.1085, r_multiple=4.13, detail=detail)
        stored = journal.read()[0]
        assert stored.r_multiple == pytest.approx(4.13)
        assert stored.outcome == Outcome.WIN.value
        assert stored.detail == detail

    def test_without_an_override_r_is_measured_against_the_plan(self, journal):
        entry = journal.record(make_signal())
        journal.close(entry.entry_id, exit_price=1.1085)
        assert journal.read()[0].r_multiple == pytest.approx(4.25)

    def test_reasons_and_warnings_survive_the_round_trip(self, journal):
        from dataclasses import replace
        from trading_bot.journal import _signal_from_dict
        from trading_bot.models import Reason

        signal = replace(
            make_signal(),
            reasons=(Reason("ADX", "ADX 31 is above the 20 trend threshold", 8.0),),
            warnings=("pip value approximate",),
        )
        journal.record(signal)
        rebuilt = _signal_from_dict(journal.read()[0].signal)
        assert rebuilt.reasons == signal.reasons
        assert rebuilt.warnings == signal.warnings


class TestRecording:
    def test_record_then_read(self, journal):
        entry = journal.record(make_signal())
        entries = journal.read()
        assert len(entries) == 1
        assert entries[0].entry_id == entry.entry_id
        assert entries[0].is_open

    def test_id_is_stable(self):
        signal = make_signal()
        assert signal_id(signal.symbol, signal.issued_at.isoformat()) == (
            f"EURUSD@{signal.issued_at.isoformat()}"
        )

    def test_record_once_deduplicates(self, journal):
        assert journal.record_once(make_signal()) is not None
        assert journal.record_once(make_signal()) is None
        assert len(journal.read()) == 1

    def test_different_bars_are_different_signals(self, journal):
        first = make_signal()
        second = replace(first, issued_at=first.issued_at + timedelta(hours=1))
        journal.record_once(first)
        journal.record_once(second)
        assert len(journal.read()) == 2


class TestClosing:
    def test_close_sets_the_outcome(self, journal):
        entry = journal.record(make_signal())
        closed = journal.close(entry.entry_id, 1.1085)
        assert closed.outcome == "win"
        assert closed.r_multiple == pytest.approx(4.25)
        assert not closed.is_open

    def test_close_survives_a_reread(self, journal):
        entry = journal.record(make_signal())
        journal.close(entry.entry_id, 1.0980)
        reloaded = journal.read()[0]
        assert reloaded.outcome == "loss"
        assert reloaded.r_multiple == pytest.approx(-1.0)
        assert reloaded.exit_price == pytest.approx(1.0980)

    def test_closing_appends_rather_than_rewrites(self, journal):
        """History must be additive: the original advice stays on disk verbatim."""
        entry = journal.record(make_signal())
        original = journal.path.read_text()
        journal.close(entry.entry_id, 1.1085)
        after = journal.path.read_text()
        assert after.startswith(original)
        assert len(after.splitlines()) == 2

    def test_double_close_is_refused(self, journal):
        entry = journal.record(make_signal())
        journal.close(entry.entry_id, 1.1085)
        with pytest.raises(DataError, match="already closed"):
            journal.close(entry.entry_id, 1.1000)

    def test_unknown_id_is_refused(self, journal):
        journal.record(make_signal())
        with pytest.raises(DataError, match="no journalled signal"):
            journal.close("NOPE@2024-01-01T00:00:00+00:00", 1.1)

    def test_naive_timestamp_is_refused(self, journal):
        entry = journal.record(make_signal())
        with pytest.raises(DataError, match="timezone-aware"):
            journal.close(entry.entry_id, 1.1085, closed_at=datetime(2024, 1, 2))

    def test_close_before_signal_in_file_still_folds(self, tmp_path):
        """A close may precede its signal in a concatenated file."""
        source = Journal(tmp_path / "a.jsonl")
        entry = source.record(make_signal())
        source.close(entry.entry_id, 1.1085)
        signal_line, close_line = source.path.read_text().splitlines()

        reordered = tmp_path / "b.jsonl"
        reordered.write_text(f"{close_line}\n{signal_line}\n")
        folded = Journal(reordered).read()
        assert len(folded) == 1
        assert folded[0].outcome == "win"


class TestLiveMetrics:
    def test_open_trades_are_excluded(self, journal):
        journal.record(make_signal())
        assert journal.live_metrics().trades == 0

    def test_closed_trades_are_measured(self, journal):
        for hours, exit_price in enumerate([1.1085, 1.0980, 1.1085]):
            signal = replace(
                make_signal(), issued_at=make_signal().issued_at + timedelta(hours=hours)
            )
            entry = journal.record(signal)
            journal.close(entry.entry_id, exit_price)

        metrics = journal.live_metrics()
        assert metrics.trades == 3
        assert metrics.wins == 2
        assert metrics.win_rate == pytest.approx(2 / 3)
        assert metrics.total_r == pytest.approx(4.25 + 4.25 - 1.0)

    def test_interval_is_reported_for_small_samples(self, journal):
        entry = journal.record(make_signal())
        journal.close(entry.entry_id, 1.1085)
        interval = journal.live_metrics().win_rate_interval
        assert interval.low < 0.5, "one winning trade must not read as a proven edge"


class TestSummary:
    def test_empty(self, journal):
        assert "No signals journalled" in journal.summary()

    def test_open_signal_prompts_for_a_close(self, journal):
        journal.record(make_signal())
        summary = journal.summary()
        assert "No closed trades yet" in summary
        assert "--close" in summary

    def test_closed_signal_shows_live_performance(self, journal):
        entry = journal.record(make_signal())
        journal.close(entry.entry_id, 1.1085)
        summary = journal.summary()
        assert "LIVE PERFORMANCE" in summary
        assert "too few to judge" in summary

    def test_corrupt_line_is_reported(self, tmp_path):
        path = tmp_path / "j.jsonl"
        path.write_text("{bad json}\n")
        with pytest.raises(DataError, match="not valid JSON"):
            Journal(path).read()
