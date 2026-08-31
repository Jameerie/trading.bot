"""End-to-end CLI behaviour.

These run the real entry point against the bundled sample data, so they catch
wiring mistakes that unit tests on individual modules cannot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.cli import build_parser, main
from trading_bot.data.csv_source import write_csv
from trading_bot.data.synthetic import generate

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "default.toml"


@pytest.fixture
def sample_csv(tmp_path):
    path = tmp_path / "EURUSD_H1.csv"
    write_csv(path, generate(bars=900, seed=42))
    return path


class TestParser:
    def test_requires_a_subcommand(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_version_exits_cleanly(self):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0


class TestScan:
    def test_runs_on_synthetic_data(self, capsys):
        assert main(["scan", "--source", "synthetic", "--symbols", "EURUSD", "--no-journal"]) == 0
        out = capsys.readouterr().out
        assert "EURUSD" in out
        assert "setup(s) found" in out

    def test_states_it_does_not_trade(self, capsys):
        main(["scan", "--source", "synthetic", "--symbols", "EURUSD", "--no-journal"])
        assert "you place the trade" in capsys.readouterr().out.lower()

    def test_handles_several_symbols(self, capsys):
        main([
            "scan", "--source", "synthetic",
            "--symbols", "EURUSD", "GBPUSD", "USDJPY", "--no-journal",
        ])
        out = capsys.readouterr().out
        for symbol in ("EURUSD", "GBPUSD", "USDJPY"):
            assert symbol in out

    def test_missing_csv_is_a_clean_message_not_a_traceback(self, capsys):
        code = main(["scan", "--source", "csv", "--symbols", "ZZZZZZ", "--no-journal"])
        assert code == 0
        assert "could not scan" in capsys.readouterr().out

    def test_journal_is_written_when_enabled(self, tmp_path, capsys):
        config = tmp_path / "c.toml"
        journal = tmp_path / "j.jsonl"
        config.write_text(
            f'journal_path = "{journal}"\n\n'
            '[data]\nsource = "synthetic"\nsymbols = ["EURUSD"]\n\n'
            '[strategy]\nmin_confluence = 0.4\n'
        )
        main(["--config", str(config), "scan"])
        # A journal file only appears if something qualified; either way the run
        # must not fail, and any file written must be readable.
        if journal.exists():
            from trading_bot.journal import Journal

            assert Journal(journal).read()


class TestBacktest:
    def test_runs_on_a_csv(self, sample_csv, capsys):
        assert main(["backtest", "--csv", str(sample_csv)]) == 0
        assert "RESULT" in capsys.readouterr().out or "No trades" in capsys.readouterr().out

    def test_split_reports_both_halves(self, sample_csv, capsys):
        assert main(["backtest", "--csv", str(sample_csv), "--split", "0.7"]) == 0
        out = capsys.readouterr().out
        assert "in-sample" in out
        assert "out-of-sample" in out

    def test_trade_listing(self, sample_csv, capsys):
        assert main(["backtest", "--csv", str(sample_csv), "--trades"]) == 0

    def test_reports_the_target(self, sample_csv, capsys):
        main(["backtest", "--csv", str(sample_csv)])
        out = capsys.readouterr().out
        assert "TARGET" in out or "No trades" in out

    def test_missing_csv_returns_an_error_code(self, capsys):
        assert main(["backtest", "--csv", "/nonexistent/file.csv"]) == 2
        assert "error:" in capsys.readouterr().err


class TestCalibrate:
    def test_produces_a_sweep(self, sample_csv, capsys):
        assert main(["calibrate", "--csv", str(sample_csv)]) == 0
        out = capsys.readouterr().out
        assert "Selectivity sweep" in out
        assert "min_confl" in out
        assert "Recommendation" in out

    def test_full_series_mode(self, sample_csv, capsys):
        assert main(["calibrate", "--csv", str(sample_csv), "--split", "0"]) == 0
        assert "full series" in capsys.readouterr().out


class TestJournalCommand:
    def test_empty_journal(self, tmp_path, capsys):
        assert main(["journal", "--path", str(tmp_path / "none.jsonl")]) == 0
        assert "No signals journalled" in capsys.readouterr().out


class TestDataCommand:
    def test_generates_files(self, tmp_path, capsys):
        code = main([
            "data", "--generate", "--symbols", "EURUSD",
            "--bars", "200", "--out", str(tmp_path),
        ])
        assert code == 0
        assert (tmp_path / "EURUSD_H1.csv").exists()

    def test_warns_that_synthetic_data_is_not_a_market(self, tmp_path, capsys):
        main(["data", "--generate", "--symbols", "EURUSD", "--bars", "100", "--out", str(tmp_path)])
        assert "not a market" in capsys.readouterr().out

    def test_inspect(self, sample_csv, capsys):
        assert main(["data", "--inspect", str(sample_csv)]) == 0
        assert "bars" in capsys.readouterr().out

    def test_no_action_returns_nonzero(self, capsys):
        assert main(["data"]) == 1


class TestRiskCommand:
    def test_assumed_rate(self, capsys):
        assert main(["risk", "--win-rate", "0.3", "--trials", "300"]) == 0
        out = capsys.readouterr().out
        assert "RECOMMENDED RISK" in out
        assert "Breakeven" in out

    def test_losing_edge_is_reported(self, capsys):
        assert main(["risk", "--win-rate", "0.1", "--trials", "300"]) == 0
        assert "loses money" in capsys.readouterr().out

    def test_from_backtest(self, sample_csv, capsys):
        """Sizing from measured trades has three legitimate outcomes.

        It can recommend a size; it can refuse because the backtest produced no
        trades; or it can report a losing edge — which is the expected result on
        a small sample, because sizing uses the *lower bound* of the win-rate
        interval and that bound often sits below breakeven. All three are the
        system working, so the test accepts any of them and only rejects a
        crash or an empty report.
        """
        code = main(["risk", "--from-backtest", "--csv", str(sample_csv), "--trials", "300"])
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert code in (0, 2)
        assert (
            "RECOMMENDED RISK" in combined
            or "no trades" in combined
            or "loses money" in combined
        ), combined[-300:]


class TestJournalCommands:
    def _config(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text(f'journal_path = "{tmp_path / "j.jsonl"}"\n')
        return str(path)

    def test_open_list_is_empty(self, tmp_path, capsys):
        assert main(["--config", self._config(tmp_path), "journal", "--open"]) == 0
        assert "No open signals" in capsys.readouterr().out

    def test_close_round_trip(self, tmp_path, capsys):
        import sys as _sys
        _sys.path.insert(0, str(REPO / "tests"))
        from trading_bot.journal import Journal
        from test_backtest import make_signal

        config = self._config(tmp_path)
        entry = Journal(tmp_path / "j.jsonl").record(make_signal())

        assert main(["--config", config, "journal", "--open"]) == 0
        assert entry.entry_id in capsys.readouterr().out

        code = main(["--config", config, "journal",
                     "--close", entry.entry_id, "--exit", "1.1085"])
        assert code == 0
        out = capsys.readouterr().out
        assert "win" in out and "+4.25R" in out

        assert main(["--config", config, "journal"]) == 0
        assert "LIVE PERFORMANCE" in capsys.readouterr().out

    def test_close_without_exit_price_is_rejected(self, tmp_path, capsys):
        code = main(["--config", self._config(tmp_path), "journal", "--close", "X@Y"])
        assert code == 2
        assert "needs --exit" in capsys.readouterr().err


class TestServeCommand:
    def test_serve_is_registered(self):
        args = build_parser().parse_args(["serve", "--port", "9999"])
        assert args.port == 9999
        assert args.host == "127.0.0.1", "serve must default to loopback"

    def test_serve_accepts_a_token(self):
        args = build_parser().parse_args(["serve", "--token", "abc"])
        assert args.token == "abc"


class TestShippedConfig:
    @pytest.mark.skipif(not CONFIG.exists(), reason="repo config not present")
    def test_scan_runs_with_the_shipped_config(self, capsys):
        code = main(["--config", str(CONFIG), "scan", "--source", "synthetic", "--no-journal"])
        assert code == 0
