"""Dukascopy historical candles over plain HTTPS: real market data, no key.

Dukascopy Bank publishes its price history as small LZMA-compressed binary
files, one per instrument per period, on a public datafeed that needs no
account. It covers every instrument in this registry, years back, down to tick
resolution. That makes it the way to get *real* candles for the whole universe
onto a machine that has nothing on it but Python.

The format is not documented by Dukascopy but is well established by the
open-source readers of it (dukascopy-node, duka, and others):

* ``.../{SYMBOL}/{YEAR}/{MONTH-1:02d}/BID_candles_hour_1.bi5`` holds one month
  of hourly candles; ``.../{YEAR}/BID_candles_day_1.bi5`` one year of daily
  candles; ``.../{YEAR}/{MONTH-1:02d}/{DAY:02d}/BID_candles_min_1.bi5`` one
  day of minute candles. Months are zero-based in the path.
* Each file is LZMA ("alone" format) over fixed 24-byte big-endian records:
  ``int32`` seconds since the file's period start, then ``open``, ``close``,
  ``low``, ``high`` as integers scaled by 10^5 (10^3 on three-decimal
  instruments such as JPY pairs and the metals), then ``float32`` volume.
* Hours the market is shut appear as flat, zero-volume records. They are
  dropped, as every reader of this format drops them.

Two honesty notes. The scale is inferred from the price magnitude as well as
the instrument's digits, so a three-decimal exotic this registry does not know
about still decodes at the right size; the check is a factor-of-a-hundred
question, and no currency has moved a hundredfold inside the window anyone
would fetch. And this module never touches the network in the test suite: the
decoder is tested on files built in the test, and the transport is the same
``urllib`` the Twelve Data source uses.
"""

from __future__ import annotations

import lzma
import math
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

from ..errors import DataError
from ..instruments import Instrument, get_instrument
from ..models import Candle, Timeframe, utc_now
from ..resample import resample
from .base import validate_series

BASE_URL = "https://datafeed.dukascopy.com/datafeed"

# One candle record: seconds offset, open, close, low, high, volume.
RECORD = struct.Struct(">iiiiif")

# How far back each walk may go before giving up. Hourly files are monthly,
# daily files yearly, minute files daily.
MAX_PERIODS = {"hour": 72, "day": 30, "min": 240}

# Rough value of one unit of each currency in US dollars, for the scale sanity
# check only. These do not need to be current; they need to be within a factor
# of thirty, and the check asks a factor-of-a-hundred question.
_USD_VALUE: dict[str, float] = {
    "USD": 1.0, "EUR": 1.1, "GBP": 1.3, "CHF": 1.1, "JPY": 0.0068, "AUD": 0.66,
    "NZD": 0.60, "CAD": 0.73, "SEK": 0.095, "NOK": 0.093, "DKK": 0.145, "PLN": 0.25,
    "CZK": 0.043, "HUF": 0.0027, "TRY": 0.03, "ZAR": 0.055, "MXN": 0.055, "SGD": 0.74,
    "HKD": 0.128, "CNH": 0.14, "THB": 0.028, "ILS": 0.27, "INR": 0.012, "BRL": 0.18,
    "XAU": 2500.0, "XAG": 30.0, "XPT": 950.0, "XPD": 950.0,
}

Fetcher = Callable[[str, float], bytes]


# ------------------------------------------------------------------ decoding


def decompress(blob: bytes) -> bytes:
    """Unpack one ``.bi5`` file. An empty body is an empty period, not an error."""
    if not blob:
        return b""
    try:
        return lzma.decompress(blob, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError:
        try:
            return lzma.decompress(blob, format=lzma.FORMAT_AUTO)
        except lzma.LZMAError as exc:
            raise DataError(f"Dukascopy file is not LZMA data: {exc}") from exc


def compress(data: bytes) -> bytes:
    """The inverse, so a test can build a file the way the datafeed serves it."""
    return lzma.compress(data, format=lzma.FORMAT_ALONE)


def records(blob: bytes) -> list[tuple[int, int, int, int, int, float]]:
    """Raw records from one file: (seconds, open, close, low, high, volume)."""
    data = decompress(blob)
    if len(data) % RECORD.size:
        raise DataError(
            f"Dukascopy file is {len(data)} bytes, not a whole number of "
            f"{RECORD.size}-byte records"
        )
    return list(RECORD.iter_unpack(data))


def expected_price(instrument: Instrument) -> float | None:
    """The order of magnitude this pair should trade at, from the value table."""
    base = _USD_VALUE.get(instrument.base)
    quote = _USD_VALUE.get(instrument.quote)
    if base is None or quote is None or quote <= 0:
        return None
    return base / quote


def choose_scale(raw_prices: list[int], instrument: Instrument) -> float:
    """10^5 or 10^3: from the instrument's digits, checked against the prices.

    Dukascopy scales every instrument by one of two factors. The registry's
    digit count picks the default; the magnitude check overrides it only when
    the default lands a hundredfold away from where the pair trades and the
    other factor lands on it. A currency that has moved thirtyfold is history;
    one that has moved a hundredfold is a decimal error.
    """
    default = 1e3 if instrument.digits <= 3 else 1e5
    other = 1e5 if default == 1e3 else 1e3
    expected = expected_price(instrument)
    positive = [p for p in raw_prices if p > 0]
    if expected is None or not positive:
        return default
    median = sorted(positive)[len(positive) // 2]
    off_default = abs(math.log10(median / default / expected))
    off_other = abs(math.log10(median / other / expected))
    if off_default > math.log10(30) and off_other < off_default:
        return other
    return default


def is_shut(record: tuple[int, int, int, int, int, float]) -> bool:
    """A flat, zero-volume record: an hour the market was closed."""
    _, o, c, low, high, volume = record
    return volume == 0 and o == c == low == high


def to_candles(
    recs: list[tuple[int, int, int, int, int, float]], period_start: datetime, scale: float
) -> list[Candle]:
    """Scale raw records into candles, dropping the hours the market was shut."""
    out: list[Candle] = []
    for record in recs:
        if is_shut(record):
            continue
        offset, o, c, low, high, volume = record
        stamp = period_start + timedelta(seconds=offset)
        try:
            out.append(Candle(stamp, o / scale, high / scale, low / scale, c / scale, float(volume)))
        except DataError as exc:
            raise DataError(f"Dukascopy record at {stamp.isoformat()} is malformed: {exc}") from exc
    return out


# ----------------------------------------------------------------- transport


def _http_get(url: str, timeout: float) -> bytes:
    """Fetch one file. A 404 is an empty period; anything else is an error."""
    request = urllib.request.Request(url, headers={"User-Agent": "trading.bot/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return b""
        raise DataError(f"Dukascopy returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise DataError(f"could not reach Dukascopy: {exc.reason}") from exc


def file_url(symbol: str, kind: str, start: datetime) -> str:
    """The datafeed path for one period of one instrument."""
    sym = symbol.upper()
    if kind == "hour":
        return f"{BASE_URL}/{sym}/{start.year}/{start.month - 1:02d}/BID_candles_hour_1.bi5"
    if kind == "day":
        return f"{BASE_URL}/{sym}/{start.year}/BID_candles_day_1.bi5"
    if kind == "min":
        return (
            f"{BASE_URL}/{sym}/{start.year}/{start.month - 1:02d}/{start.day:02d}/"
            f"BID_candles_min_1.bi5"
        )
    raise ValueError(f"unknown file kind {kind!r}")


def periods_backwards(kind: str, now: datetime) -> Iterator[datetime]:
    """Period start times, newest first, for the file kind."""
    now = now.astimezone(timezone.utc)
    if kind == "hour":
        cursor = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        while True:
            yield cursor
            cursor = (cursor - timedelta(days=1)).replace(day=1)
    elif kind == "day":
        cursor = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        while True:
            yield cursor
            cursor = cursor.replace(year=cursor.year - 1)
    elif kind == "min":
        cursor = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        while True:
            yield cursor
            cursor -= timedelta(days=1)
    else:
        raise ValueError(f"unknown file kind {kind!r}")


def _plan(timeframe: Timeframe, limit: int) -> tuple[str, int, Timeframe | None]:
    """Which files to read for a timeframe, how many bars to gather, what to resample to."""
    if timeframe is Timeframe.H1:
        return "hour", limit, None
    if timeframe is Timeframe.H4:
        return "hour", limit * 4 + 8, Timeframe.H4
    if timeframe is Timeframe.D1:
        return "day", limit, None
    if timeframe in (Timeframe.M5, Timeframe.M15, Timeframe.M30):
        return "min", limit * timeframe.minutes + timeframe.minutes * 2, timeframe
    raise DataError(f"{timeframe.name} is not supported by the Dukascopy source")


class DukascopySource:
    """DataSource over the Dukascopy datafeed.

    ``fetcher`` and ``now`` exist so the walk can be tested without a network:
    the suite hands in a function that serves files it built itself.
    """

    def __init__(
        self,
        timeout: float = 20.0,
        pause: float = 0.2,
        fetcher: Fetcher | None = None,
        now: datetime | None = None,
    ) -> None:
        self.timeout = timeout
        self.pause = pause
        self._fetch_bytes = fetcher or _http_get
        self._now = now
        self.requests = 0

    def _get(self, url: str) -> bytes:
        # A short, unconditional pause between files. The datafeed tolerates
        # readers that behave; sixty instruments times ten months is six
        # hundred requests, and they need not arrive as a burst.
        if self.pause > 0 and self.requests:
            time.sleep(self.pause)
        self.requests += 1
        return self._fetch_bytes(url, self.timeout)

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        kind, needed, target = _plan(timeframe, max(limit, 1))
        instrument = get_instrument(symbol)
        now = self._now or utc_now()

        # Gather raw records period by period, newest first, until there are
        # enough; the scale is chosen once over everything gathered.
        gathered: list[tuple[datetime, list]] = []
        total = 0
        seen_data = False
        empty_run = 0
        for count, start in enumerate(periods_backwards(kind, now)):
            if count >= MAX_PERIODS[kind] or total >= needed:
                break
            recs = records(self._get(file_url(symbol, kind, start)))
            if not recs:
                empty_run += 1
                # Before any data: the current period may simply not exist yet,
                # so allow a few. After data: a gap this long means the history
                # has run out, or the instrument was listed later than this.
                if empty_run >= (4 if seen_data else 6):
                    break
                continue
            empty_run = 0
            seen_data = True
            gathered.append((start, recs))
            # Count tradable hours only: a month is a third weekend, and a walk
            # that counted the flats would stop a month short of what was asked.
            total += sum(1 for record in recs if not is_shut(record))

        if not gathered:
            raise DataError(
                f"Dukascopy has no {kind}ly candles for {symbol.upper()} in the "
                f"{MAX_PERIODS[kind]} most recent periods. Check the symbol is one the "
                f"datafeed carries (it uses plain six-letter names such as EURUSD, XAUUSD)."
            )

        scale = choose_scale([r[2] for _, recs in gathered for r in recs], instrument)
        candles: list[Candle] = []
        for start, recs in reversed(gathered):
            candles.extend(to_candles(recs, start, scale))
        candles.sort(key=lambda c: c.timestamp)
        validate_series(candles, symbol.upper())
        if target is not None:
            candles = resample(candles, target)
        if not candles:
            raise DataError(f"no complete {timeframe.name} bars could be built for {symbol}")
        return candles[-limit:]
