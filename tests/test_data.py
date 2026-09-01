"""Data loading, resampling and the synthetic generator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.data.base import missing_symbols, validate_series
from trading_bot.data.csv_source import (
    CsvSource,
    fill_commands,
    fill_directory,
    load_csv,
    parse_timestamp,
    write_csv,
)
from trading_bot.data.synthetic import SyntheticSource, generate, generate_trending
from trading_bot.errors import DataError
from trading_bot.models import Candle, Timeframe
from trading_bot.resample import bucket_start, htf_closed_before, resample

from conftest import START, make_candle


class TestValidateSeries:
    def test_rejects_empty(self):
        with pytest.raises(DataError, match="empty"):
            validate_series([])

    def test_rejects_duplicate_timestamps(self):
        candles = [make_candle(0, 1.1, 1.2, 1.0, 1.15), make_candle(0, 1.1, 1.2, 1.0, 1.15)]
        with pytest.raises(DataError, match="duplicate"):
            validate_series(candles)

    def test_rejects_out_of_order(self):
        candles = [make_candle(5, 1.1, 1.2, 1.0, 1.15), make_candle(1, 1.1, 1.2, 1.0, 1.15)]
        with pytest.raises(DataError, match="out-of-order"):
            validate_series(candles)


class TestTimestampParsing:
    @pytest.mark.parametrize(
        "text",
        [
            "2024-01-15 13:00:00",
            "2024-01-15T13:00:00",
            "2024-01-15T13:00:00Z",
            "2024-01-15 13:00",
            "15/01/2024 13:00:00",
            "15.01.2024 13:00:00",
            "2024.01.15 13:00",
        ],
    )
    def test_common_vendor_formats(self, text):
        parsed = parse_timestamp(text)
        assert parsed.tzinfo is not None
        assert parsed.year == 2024 and parsed.month == 1 and parsed.day == 15

    def test_epoch_seconds(self):
        assert parse_timestamp("1705323600").year == 2024

    def test_epoch_milliseconds(self):
        assert parse_timestamp("1705323600000").year == 2024

    def test_rejects_gibberish(self):
        with pytest.raises(DataError, match="unrecognised timestamp"):
            parse_timestamp("not a date")

    def test_naive_input_becomes_utc(self):
        assert parse_timestamp("2024-01-15 13:00:00").tzinfo == timezone.utc


class TestCsv:
    def test_round_trip(self, tmp_path):
        original = generate(bars=50, seed=1)
        path = tmp_path / "EURUSD_H1.csv"
        write_csv(path, original)
        restored = load_csv(path)
        assert len(restored) == len(original)
        for a, b in zip(original, restored):
            assert a.timestamp == b.timestamp
            assert a.close == pytest.approx(b.close)

    def test_accepts_alternative_headers(self, tmp_path):
        path = tmp_path / "alt.csv"
        path.write_text("Date,O,H,L,C,Vol\n2024-01-01 00:00:00,1.1,1.2,1.0,1.15,100\n")
        candles = load_csv(path)
        assert len(candles) == 1
        assert candles[0].close == pytest.approx(1.15)

    def test_volume_is_optional(self, tmp_path):
        path = tmp_path / "novol.csv"
        path.write_text("timestamp,open,high,low,close\n2024-01-01 00:00:00,1.1,1.2,1.0,1.15\n")
        assert load_csv(path)[0].volume == 0.0

    def test_missing_column_is_reported(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("timestamp,open,high\n2024-01-01,1.1,1.2\n")
        with pytest.raises(DataError, match="missing column"):
            load_csv(path)

    def test_bad_row_names_its_line(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text(
            "timestamp,open,high,low,close\n"
            "2024-01-01 00:00:00,1.1,1.2,1.0,1.15\n"
            "2024-01-01 01:00:00,9.9,1.2,1.0,1.15\n"  # open outside range
        )
        with pytest.raises(DataError, match="line 3"):
            load_csv(path)

    def test_blank_lines_are_skipped(self, tmp_path):
        path = tmp_path / "blank.csv"
        path.write_text(
            "timestamp,open,high,low,close\n"
            "2024-01-01 00:00:00,1.1,1.2,1.0,1.15\n"
            "\n"
            "2024-01-01 01:00:00,1.15,1.25,1.05,1.2\n"
        )
        assert len(load_csv(path)) == 2

    def test_missing_file(self, tmp_path):
        with pytest.raises(DataError, match="not found"):
            load_csv(tmp_path / "nope.csv")

    def test_limit_returns_the_most_recent(self, tmp_path):
        original = generate(bars=50, seed=1)
        path = tmp_path / "EURUSD_H1.csv"
        write_csv(path, original)
        assert load_csv(path, limit=10)[-1].timestamp == original[-1].timestamp

    def test_source_finds_named_file(self, tmp_path):
        write_csv(tmp_path / "EURUSD_H1.csv", generate(bars=30, seed=2))
        candles = CsvSource(tmp_path).fetch("EURUSD", Timeframe.H1, 30)
        assert len(candles) == 30

    def test_source_reports_what_it_looked_for(self, tmp_path):
        with pytest.raises(DataError, match="Looked for"):
            CsvSource(tmp_path).fetch("GBPUSD", Timeframe.H1, 30)

    def test_source_says_what_to_do_about_it(self, tmp_path):
        """Naming the file it wanted is half an answer; the other half is the fix."""
        with pytest.raises(DataError, match="data --fetch --symbols GBPUSD"):
            CsvSource(tmp_path).fetch("GBPUSD", Timeframe.H1, 30)


class TestMissingFiles:
    """Which symbols have nothing on disk, asked before a single fetch is tried."""

    def test_missing_lists_only_the_absent_ones_in_order(self, tmp_path):
        write_csv(tmp_path / "EURUSD_H1.csv", generate(bars=30, seed=1))
        source = CsvSource(tmp_path)
        asked = ["EURNOK", "EURUSD", "XAUUSD"]
        assert source.missing(asked, Timeframe.H1) == ["EURNOK", "XAUUSD"]

    def test_missing_is_per_timeframe(self, tmp_path):
        write_csv(tmp_path / "EURUSD_H1.csv", generate(bars=30, seed=1))
        source = CsvSource(tmp_path)
        assert source.missing(["EURUSD"], Timeframe.H1) == []
        assert source.missing(["EURUSD"], Timeframe.H4) == ["EURUSD"]

    @pytest.mark.parametrize("name", ["EURUSD_H1.csv", "EURUSDH1.csv", "eurusd_h1.csv"])
    def test_every_accepted_spelling_counts_as_present(self, tmp_path, name):
        write_csv(tmp_path / name, generate(bars=30, seed=1))
        source = CsvSource(tmp_path)
        assert source.missing(["EURUSD"], Timeframe.H1) == []
        assert source.path_for("EURUSD", Timeframe.H1).name == name

    def test_a_streaming_source_is_not_asked(self):
        """Only a directory can answer this for free. An API would have to pay."""
        assert missing_symbols(SyntheticSource(), ["EURUSD", "EURNOK"], Timeframe.H1) == []

    def test_the_advice_is_defined_once(self):
        commands = fill_commands(Timeframe.H1, only_missing=True)
        assert [c for c, _ in commands] == [
            "python -m trading_bot data --fetch --only-missing --timeframe H1",
            "python -m trading_bot data --generate --only-missing --timeframe H1",
        ]
        # Market data first: only one of the two can tell you about a market.
        assert "not a market" in commands[1][1]


class TestFillDirectory:
    def test_writes_one_file_per_symbol(self, tmp_path):
        results = list(fill_directory(
            SyntheticSource(), ["EURUSD", "USDJPY"], Timeframe.H1, tmp_path, bars=120
        ))
        assert [r.status for r in results] == ["written", "written"]
        assert [r.bars for r in results] == [120, 120]
        assert (tmp_path / "EURUSD_H1.csv").exists()
        assert (tmp_path / "USDJPY_H1.csv").exists()

    def test_only_missing_leaves_existing_files_untouched(self, tmp_path):
        write_csv(tmp_path / "EURUSD_H1.csv", generate(bars=30, seed=1))
        before = (tmp_path / "EURUSD_H1.csv").read_bytes()
        results = list(fill_directory(
            SyntheticSource(), ["EURUSD", "USDJPY"], Timeframe.H1, tmp_path,
            bars=120, only_missing=True,
        ))
        assert [r.status for r in results] == ["skipped", "written"]
        assert (tmp_path / "EURUSD_H1.csv").read_bytes() == before

    def test_refresh_overwrites_the_file_that_backs_the_symbol(self, tmp_path):
        """Writing the canonical name beside an odd one would leave the stale
        file in front: ``path_for`` returns the canonical spelling first."""
        write_csv(tmp_path / "eurusd_h1.csv", generate(bars=30, seed=1))
        list(fill_directory(SyntheticSource(), ["EURUSD"], Timeframe.H1, tmp_path, bars=120))
        assert not (tmp_path / "EURUSD_H1.csv").exists()
        assert len(load_csv(tmp_path / "eurusd_h1.csv")) == 120

    def test_one_failure_does_not_abandon_the_rest(self, tmp_path):
        class Flaky:
            def fetch(self, symbol, timeframe, limit):
                if symbol == "EURNOK":
                    raise DataError("provider does not carry EURNOK")
                return generate(bars=limit, seed=3)

        results = list(fill_directory(
            Flaky(), ["EURNOK", "EURUSD"], Timeframe.H1, tmp_path, bars=90
        ))
        assert [r.status for r in results] == ["failed", "written"]
        assert "does not carry" in results[0].message
        assert (tmp_path / "EURUSD_H1.csv").exists()

    def test_creates_the_directory(self, tmp_path):
        target = tmp_path / "does" / "not" / "exist"
        list(fill_directory(SyntheticSource(), ["EURUSD"], Timeframe.H1, target, bars=80))
        assert (target / "EURUSD_H1.csv").exists()

    def test_what_it_wrote_reads_back(self, tmp_path):
        list(fill_directory(SyntheticSource(), ["USDJPY"], Timeframe.H1, tmp_path, bars=100))
        candles = CsvSource(tmp_path).fetch("USDJPY", Timeframe.H1, 100)
        assert len(candles) == 100
        assert all(c.timestamp.tzinfo is not None for c in candles)


class TestSynthetic:
    def test_is_deterministic(self):
        a = generate(bars=100, seed=42)
        b = generate(bars=100, seed=42)
        assert [c.close for c in a] == [c.close for c in b]

    def test_different_seeds_differ(self):
        a = generate(bars=100, seed=1)
        b = generate(bars=100, seed=2)
        assert [c.close for c in a] != [c.close for c in b]

    def test_produces_valid_candles(self):
        for candle in generate(bars=300, seed=5):
            assert candle.low <= candle.open <= candle.high
            assert candle.low <= candle.close <= candle.high

    def test_spacing_matches_the_timeframe(self):
        candles = generate(bars=10, timeframe=Timeframe.H4)
        assert candles[1].timestamp - candles[0].timestamp == timedelta(hours=4)

    def test_trending_series_actually_trends(self):
        candles = generate_trending(bars=400, seed=7)
        assert candles[-1].close > candles[0].close

    def test_trending_series_still_pulls_back(self):
        """Without pullbacks there are no swing points and nothing to test."""
        from trading_bot.structure import swing_points

        assert len(swing_points(generate_trending(bars=400, seed=7), 2, 2)) > 10

    def test_rejects_zero_bars(self):
        with pytest.raises(ValueError):
            generate(bars=0)

    def test_source_varies_by_symbol(self):
        source = SyntheticSource(seed=3)
        a = source.fetch("EURUSD", Timeframe.H1, 60)
        b = source.fetch("GBPUSD", Timeframe.H1, 60)
        assert [c.close for c in a] != [c.close for c in b]

    def test_jpy_symbols_get_jpy_prices(self):
        candles = SyntheticSource().fetch("USDJPY", Timeframe.H1, 50)
        assert candles[0].close > 50


class TestResample:
    def test_bucket_start_floors_to_the_window(self):
        moment = datetime(2024, 1, 15, 13, 37, tzinfo=timezone.utc)
        assert bucket_start(moment, 240) == datetime(2024, 1, 15, 12, tzinfo=timezone.utc)

    def test_ohlc_aggregation(self):
        candles = generate(bars=8, seed=1, timeframe=Timeframe.H1)
        h4 = resample(candles, Timeframe.H4)
        assert len(h4) == 2
        assert h4[0].open == candles[0].open
        assert h4[0].close == candles[3].close
        assert h4[0].high == max(c.high for c in candles[:4])
        assert h4[0].low == min(c.low for c in candles[:4])
        assert h4[0].volume == pytest.approx(sum(c.volume for c in candles[:4]))

    def test_partial_final_bucket_is_dropped(self):
        candles = generate(bars=6, seed=1, timeframe=Timeframe.H1)
        assert len(resample(candles, Timeframe.H4)) == 1

    def test_cannot_resample_downward(self):
        candles = generate(bars=20, seed=1, timeframe=Timeframe.H4)
        with pytest.raises(DataError, match="smaller than the source"):
            resample(candles, Timeframe.H1)

    def test_empty_input(self):
        assert resample([], Timeframe.H4) == []

    def test_htf_bars_close_before_the_decision_bar(self):
        candles = generate(bars=200, seed=1, timeframe=Timeframe.H1)
        for index in (10, 50, 120):
            decision = candles[index].timestamp
            for bar in htf_closed_before(candles, index, Timeframe.H4):
                assert bar.timestamp + timedelta(hours=4) <= decision

    def test_htf_rejects_a_bad_index(self):
        candles = generate(bars=20, seed=1)
        with pytest.raises(IndexError):
            htf_closed_before(candles, 99, Timeframe.H4)
