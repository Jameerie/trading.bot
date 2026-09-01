"""The Dukascopy datafeed reader, exercised on files built here.

No test touches the network. The format is fixed 24-byte records under LZMA,
so a file can be assembled in a few lines and the decoder checked against what
was put in: the timestamps, the scale, the dropped weekend flats, and the walk
that stitches monthly files into one series.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.config import load_config
from trading_bot.data.dukascopy import (
    RECORD,
    DukascopySource,
    choose_scale,
    compress,
    expected_price,
    file_url,
    periods_backwards,
    records,
    to_candles,
)
from trading_bot.data.rest_source import build_rest_source
from trading_bot.errors import DataError
from trading_bot.instruments import get_instrument
from trading_bot.models import Timeframe

UTC = timezone.utc


def build_file(rows):
    """Pack (seconds, open, close, low, high, volume) rows the way the feed does."""
    return compress(b"".join(RECORD.pack(*row) for row in rows))


def month_of_hours(start: datetime, price: int = 110000, step: int = 7):
    """A month of hourly rows with flat zero-volume weekends, like the real files."""
    rows = []
    days = ((start.replace(month=start.month % 12 + 1, year=start.year + (start.month == 12))
             - start).days)
    for h in range(24 * days):
        stamp = start + timedelta(hours=h)
        if stamp.weekday() >= 5:
            rows.append((h * 3600, price, price, price, price, 0.0))
            continue
        o, c = price, price + (step if h % 3 else -step)
        rows.append((h * 3600, o, c, min(o, c) - 3, max(o, c) + 4, 10.0 + h % 5))
        price = c
    return rows


class TestDecoding:
    def test_records_come_back_in_the_order_they_were_packed(self):
        rows = [(0, 110000, 110050, 109990, 110070, 12.5), (3600, 110050, 110020, 110000, 110060, 3.0)]
        assert records(build_file(rows)) == [pytest.approx(r) for r in rows]

    def test_timestamps_are_seconds_from_the_period_start(self):
        start = datetime(2025, 8, 1, tzinfo=UTC)
        rows = [(5 * 3600, 110000, 110050, 109990, 110070, 1.0)]
        candle = to_candles(records(build_file(rows)), start, 1e5)[0]
        assert candle.timestamp == start + timedelta(hours=5)
        assert candle.open == pytest.approx(1.10000)
        assert candle.close == pytest.approx(1.10050)
        assert candle.low == pytest.approx(1.09990)
        assert candle.high == pytest.approx(1.10070)

    def test_flat_zero_volume_bars_are_dropped(self):
        start = datetime(2025, 8, 1, tzinfo=UTC)
        candles = to_candles(records(build_file(month_of_hours(start))), start, 1e5)
        assert candles
        assert all(c.timestamp.weekday() < 5 for c in candles)

    def test_an_empty_body_is_an_empty_period(self):
        assert records(b"") == []

    def test_garbage_is_reported_not_decoded(self):
        with pytest.raises(DataError, match="not LZMA"):
            records(b"this is not a compressed file")

    def test_a_torn_file_is_reported(self):
        torn = compress(RECORD.pack(0, 1, 2, 1, 2, 1.0)[:-4])
        with pytest.raises(DataError, match="whole number"):
            records(torn)

    def test_a_malformed_record_names_its_time(self):
        start = datetime(2025, 8, 1, tzinfo=UTC)
        bad = [(0, 110000, 110050, 110100, 109900, 1.0)]  # low above high
        with pytest.raises(DataError, match="2025-08-01"):
            to_candles(records(build_file(bad)), start, 1e5)


class TestScale:
    def test_five_decimal_pairs_scale_by_ten_to_the_five(self):
        assert choose_scale([110000, 110500], get_instrument("EURUSD")) == 1e5

    def test_three_decimal_pairs_scale_by_ten_to_the_three(self):
        assert choose_scale([150123, 150200], get_instrument("USDJPY")) == 1e3
        assert choose_scale([2450123], get_instrument("XAUUSD")) == 1e3

    def test_a_three_decimal_exotic_the_registry_calls_five_is_caught(self):
        """USDTHB trades near 35. Decoded at 10^5 it would read 0.35; the check fixes it."""
        thb = get_instrument("USDTHB")
        assert choose_scale([35123, 35300], thb) == 1e3
        assert choose_scale([3512300, 3530000], thb) == 1e5

    def test_a_currency_that_moved_thirtyfold_is_still_history_not_a_decimal_error(self):
        """USDTRY at 1.5 in 2010 against a table value near 33 must keep its scale."""
        assert choose_scale([150000, 152000], get_instrument("USDTRY")) == 1e5

    def test_unknown_currencies_fall_back_to_digits(self):
        exotic = get_instrument("USDXYZ")  # uncatalogued: 5-digit default
        assert expected_price(exotic) is None
        assert choose_scale([12345], exotic) == 1e5

    def test_empty_prices_fall_back_to_digits(self):
        assert choose_scale([], get_instrument("USDJPY")) == 1e3


class TestPaths:
    def test_months_are_zero_based_in_the_path(self):
        assert file_url("EURUSD", "hour", datetime(2025, 8, 1, tzinfo=UTC)).endswith(
            "/EURUSD/2025/07/BID_candles_hour_1.bi5"
        )
        assert file_url("XAUUSD", "min", datetime(2025, 1, 9, tzinfo=UTC)).endswith(
            "/XAUUSD/2025/00/09/BID_candles_min_1.bi5"
        )
        assert file_url("GBPUSD", "day", datetime(2024, 1, 1, tzinfo=UTC)).endswith(
            "/GBPUSD/2024/BID_candles_day_1.bi5"
        )

    def test_periods_walk_backwards_from_now(self):
        now = datetime(2025, 3, 15, 12, tzinfo=UTC)
        months = [p for _, p in zip(range(4), periods_backwards("hour", now))]
        assert [m.strftime("%Y-%m") for m in months] == ["2025-03", "2025-02", "2025-01", "2024-12"]
        years = [p.year for _, p in zip(range(2), periods_backwards("day", now))]
        assert years == [2025, 2024]
        days = [p.day for _, p in zip(range(3), periods_backwards("min", now))]
        assert days == [15, 14, 13]

    def test_unknown_kind_is_an_error(self):
        with pytest.raises(ValueError):
            file_url("EURUSD", "week", datetime(2025, 1, 1, tzinfo=UTC))


class _Feed:
    """A fake datafeed: a dict of url -> bytes, counting what was asked for."""

    def __init__(self, files):
        self.files = files
        self.asked = []

    def __call__(self, url, timeout):
        self.asked.append(url)
        return self.files.get(url, b"")


@pytest.fixture
def two_months():
    july = datetime(2025, 7, 1, tzinfo=UTC)
    august = datetime(2025, 8, 1, tzinfo=UTC)
    return _Feed({
        file_url("EURUSD", "hour", july): build_file(month_of_hours(july, price=109000)),
        file_url("EURUSD", "hour", august): build_file(month_of_hours(august, price=110000)),
    })


class TestWalk:
    def test_stitches_months_newest_first_and_returns_the_last_n(self, two_months):
        source = DukascopySource(pause=0, fetcher=two_months, now=datetime(2025, 9, 2, tzinfo=UTC))
        candles = source.fetch("EURUSD", Timeframe.H1, 600)
        assert len(candles) == 600
        assert candles[-1].timestamp.month == 8
        assert candles[0].timestamp.month == 7
        stamps = [c.timestamp for c in candles]
        assert stamps == sorted(stamps)
        # September had no file yet, then two months of data: three requests.
        assert source.requests == 3

    def test_stops_once_it_has_enough(self, two_months):
        source = DukascopySource(pause=0, fetcher=two_months, now=datetime(2025, 9, 2, tzinfo=UTC))
        candles = source.fetch("EURUSD", Timeframe.H1, 50)
        assert len(candles) == 50
        assert source.requests == 2  # the empty September file, then August alone

    def test_h4_is_resampled_from_hourly_files(self, two_months):
        source = DukascopySource(pause=0, fetcher=two_months, now=datetime(2025, 9, 2, tzinfo=UTC))
        candles = source.fetch("EURUSD", Timeframe.H4, 30)
        assert len(candles) == 30
        assert all(c.timestamp.hour % 4 == 0 for c in candles)

    def test_sub_hour_frames_come_from_daily_minute_files(self):
        day = datetime(2025, 8, 6, tzinfo=UTC)  # a Wednesday
        rows = []
        price = 110000
        for m in range(24 * 60):
            o, c = price, price + (1 if m % 2 else -1)
            rows.append((m * 60, o, c, min(o, c) - 1, max(o, c) + 1, 1.0))
            price = c
        feed = _Feed({file_url("EURUSD", "min", day): build_file(rows)})
        source = DukascopySource(pause=0, fetcher=feed, now=day + timedelta(days=1, hours=3))
        candles = source.fetch("EURUSD", Timeframe.M15, 20)
        assert len(candles) == 20
        assert all(c.timestamp.minute % 15 == 0 for c in candles)

    def test_daily_frames_come_from_yearly_files(self):
        year = datetime(2024, 1, 1, tzinfo=UTC)
        rows = []
        price = 110000
        for d in range(200):
            stamp = year + timedelta(days=d)
            if stamp.weekday() >= 5:
                continue
            o, c = price, price + (50 if d % 2 else -30)
            rows.append((d * 86400, o, c, min(o, c) - 20, max(o, c) + 25, 100.0))
            price = c
        feed = _Feed({file_url("EURUSD", "day", year): build_file(rows)})
        source = DukascopySource(pause=0, fetcher=feed, now=datetime(2024, 8, 1, tzinfo=UTC))
        candles = source.fetch("EURUSD", Timeframe.D1, 40)
        assert len(candles) == 40
        assert (candles[1].timestamp - candles[0].timestamp) >= timedelta(days=1)

    def test_gives_up_cleanly_when_the_feed_has_nothing(self):
        feed = _Feed({})
        source = DukascopySource(pause=0, fetcher=feed, now=datetime(2025, 9, 2, tzinfo=UTC))
        with pytest.raises(DataError, match="EURXYZ"):
            source.fetch("EURXYZ", Timeframe.H1, 100)
        assert len(feed.asked) <= 6, "a dead symbol is not walked back for years"

    def test_the_series_is_validated_at_the_boundary(self, two_months):
        source = DukascopySource(pause=0, fetcher=two_months, now=datetime(2025, 9, 2, tzinfo=UTC))
        candles = source.fetch("EURUSD", Timeframe.H1, 100)
        for previous, current in zip(candles, candles[1:]):
            assert current.timestamp > previous.timestamp


class TestWiring:
    def test_dukascopy_is_the_shipped_provider_and_needs_no_key(self):
        assert load_config(None).data.provider == "dukascopy"
        assert isinstance(build_rest_source("dukascopy", None), DukascopySource)

    def test_twelve_data_still_needs_its_key(self):
        with pytest.raises(DataError, match="API key"):
            build_rest_source("twelvedata", None)

    def test_unknown_providers_name_the_supported_ones(self):
        with pytest.raises(DataError, match="dukascopy, twelvedata"):
            build_rest_source("oanda", None)
