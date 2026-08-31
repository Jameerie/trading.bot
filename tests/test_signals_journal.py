"""Signal construction, rendering, the journal and the strategy layer."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from trading_bot.instruments import get_instrument
from trading_bot.journal import Journal
from trading_bot.models import Direction, Reason, Signal, Timeframe
from trading_bot.scanner import scan_latest, scan_range
from trading_bot.signals import format_signal, format_signal_compact, no_signal_message
from trading_bot.strategy import REGISTRY, get_strategy
from trading_bot.strategy.confluence import Check, ConfluenceEngine
from trading_bot.errors import StrategyError

from test_backtest import make_signal


class TestSignalModel:
    def test_confidence_is_a_fraction_of_the_maximum(self):
        signal = replace(make_signal(), score=61.0, max_score=122.0)
        assert signal.confidence == pytest.approx(0.5)

    def test_zero_max_score_does_not_divide_by_zero(self):
        assert replace(make_signal(), score=0.0, max_score=0.0).confidence == 0.0

    @pytest.mark.parametrize(
        "fraction,grade", [(0.9, "A+"), (0.78, "A"), (0.68, "B"), (0.58, "C"), (0.3, "D")]
    )
    def test_grades(self, fraction, grade):
        signal = replace(make_signal(), score=fraction * 100, max_score=100.0)
        assert signal.grade == grade

    def test_serialises_to_plain_json(self):
        signal = replace(
            make_signal(),
            reasons=(Reason("BOS", "break of structure", 15.0),),
            warnings=("check the spread",),
        )
        encoded = json.dumps(signal.to_dict())
        decoded = json.loads(encoded)
        assert decoded["direction"] == "long"
        assert decoded["timeframe"] == "H1"
        assert decoded["reasons"][0]["code"] == "BOS"
        assert decoded["warnings"] == ["check the spread"]

    def test_is_immutable(self):
        with pytest.raises(Exception):
            make_signal().entry = 1.5


class TestFormatting:
    def test_card_contains_the_actionable_numbers(self):
        signal = replace(make_signal(), reasons=(Reason("BOS", "break of structure", 15.0),))
        text = format_signal(signal, get_instrument("EURUSD"))
        assert "BUY " in text
        assert "1.10000" in text  # entry
        assert "1.09800" in text  # stop
        assert "1.10850" in text  # target
        assert "WHAT TO DO" in text
        assert "break of structure" in text

    def test_card_states_that_it_does_not_trade(self):
        text = format_signal(make_signal(), get_instrument("EURUSD"))
        assert "does not and will not" in text

    def test_short_card_says_sell(self):
        signal = replace(make_signal(), direction=Direction.SHORT)
        assert "SELL" in format_signal(signal, get_instrument("EURUSD"))

    def test_warnings_are_shown(self):
        signal = replace(make_signal(), warnings=("liquidity is thin",))
        text = format_signal(signal, get_instrument("EURUSD"))
        assert "CHECK BEFORE YOU TAKE IT" in text
        assert "liquidity is thin" in text

    def test_compact_form_is_one_line(self):
        assert "\n" not in format_signal_compact(make_signal(), get_instrument("EURUSD"))

    def test_no_signal_message_reports_the_best_score(self):
        assert "62%" in no_signal_message("EURUSD", "H1", 0.62)

    def test_jpy_prices_use_three_decimals(self):
        signal = replace(
            make_signal(), symbol="USDJPY", entry=150.0, stop_loss=149.7, take_profit=151.2
        )
        assert "150.000" in format_signal(signal, get_instrument("USDJPY"))


class TestConfluenceEngine:
    def test_scores_sum_the_weights_that_fired(self):
        engine = ConfluenceEngine(
            [
                Check("A", 10.0, lambda c, d: (True, "a fired")),
                Check("B", 5.0, lambda c, d: (False, "")),
                Check("C", 3.0, lambda c, d: (True, "c fired")),
            ]
        )
        result = engine.score(None, Direction.LONG)
        assert result.score == pytest.approx(13.0)
        assert result.max_score == pytest.approx(18.0)
        assert result.fraction == pytest.approx(13 / 18)
        assert result.missing == ("B",)

    def test_reasons_match_the_score(self):
        """The printed rationale and the number must never disagree."""
        engine = ConfluenceEngine(
            [Check("A", 10.0, lambda c, d: (True, "a")), Check("B", 7.0, lambda c, d: (True, "b"))]
        )
        result = engine.score(None, Direction.LONG)
        assert sum(r.weight for r in result.reasons) == pytest.approx(result.score)

    def test_rejects_duplicate_codes(self):
        with pytest.raises(ValueError, match="duplicate"):
            ConfluenceEngine(
                [Check("A", 1.0, lambda c, d: (True, "")), Check("A", 1.0, lambda c, d: (True, ""))]
            )

    def test_rejects_an_empty_engine(self):
        with pytest.raises(ValueError):
            ConfluenceEngine([])


class TestStrategyRegistry:
    def test_default_strategy_resolves(self):
        assert get_strategy("trend_pullback") is not None

    def test_unknown_strategy_lists_the_options(self):
        with pytest.raises(StrategyError, match="Available"):
            get_strategy("magic_beans")

    def test_registry_is_not_empty(self):
        assert REGISTRY


class TestScanning:
    def test_scan_latest_uses_the_final_bar(self, random_series, config):
        evaluation = scan_latest(random_series, "EURUSD", config)
        assert evaluation.index == len(random_series) - 1

    def test_scan_latest_rejects_empty_data(self, config):
        with pytest.raises(ValueError):
            scan_latest([], "EURUSD", config)

    def test_every_signal_clears_the_floor(self, random_series, config):
        for evaluation in scan_range(random_series, "EURUSD", config):
            if evaluation.has_signal:
                assert evaluation.signal.risk_reward >= config.risk.min_risk_reward

    def test_every_signal_has_a_stop_on_the_correct_side(self, random_series, config):
        for evaluation in scan_range(random_series, "EURUSD", config):
            signal = evaluation.signal
            if signal is None:
                continue
            if signal.direction is Direction.LONG:
                assert signal.stop_loss < signal.entry < signal.take_profit
            else:
                assert signal.stop_loss > signal.entry > signal.take_profit

    def test_every_signal_is_explained(self, random_series, config):
        for evaluation in scan_range(random_series, "EURUSD", config):
            if evaluation.has_signal:
                assert evaluation.signal.reasons, "a signal with no rationale is not usable"

    def test_raising_the_threshold_never_adds_signals(self, random_series, config):
        loose = replace(config, strategy=replace(config.strategy, min_confluence=0.5))
        strict = replace(config, strategy=replace(config.strategy, min_confluence=0.9))
        loose_count = sum(1 for e in scan_range(random_series, "EURUSD", loose) if e.has_signal)
        strict_count = sum(1 for e in scan_range(random_series, "EURUSD", strict) if e.has_signal)
        assert strict_count <= loose_count

    def test_position_size_is_always_positive(self, random_series, config):
        for evaluation in scan_range(random_series, "EURUSD", config):
            if evaluation.has_signal:
                assert evaluation.signal.position_lots > 0


class TestJournal:
    def test_records_and_reads_back(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.record(make_signal())
        entries = journal.read()
        assert len(entries) == 1
        assert entries[0].symbol == "EURUSD"

    def test_appends_rather_than_overwrites(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.record(make_signal())
        journal.record(replace(make_signal(), direction=Direction.SHORT))
        assert len(journal.read()) == 2

    def test_creates_the_directory(self, tmp_path):
        journal = Journal(tmp_path / "deep" / "nested" / "j.jsonl")
        journal.record(make_signal())
        assert journal.path.exists()

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert Journal(tmp_path / "absent.jsonl").read() == []

    def test_corrupt_line_is_reported_not_ignored(self, tmp_path):
        path = tmp_path / "j.jsonl"
        path.write_text("{not json}\n")
        with pytest.raises(Exception, match="not valid JSON"):
            Journal(path).read()

    def test_summary_handles_an_empty_journal(self, tmp_path):
        assert "No signals journalled" in Journal(tmp_path / "j.jsonl").summary()

    def test_summary_lists_entries(self, tmp_path):
        journal = Journal(tmp_path / "j.jsonl")
        journal.record(make_signal())
        summary = journal.summary()
        assert "EURUSD" in summary
        assert "1 signal(s) recorded" in summary
