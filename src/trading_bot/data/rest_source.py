"""REST market data via the standard library.

Deliberately dependency-free (``urllib`` rather than ``requests``) so the core
package stays installable anywhere. This module is never exercised by the test
suite — tests use ``synthetic`` — because a test that needs the network is a
test that fails for reasons unrelated to the code.

API keys come from the environment only. Never put one in a config file.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..errors import DataError
from ..models import Candle, Timeframe
from .base import validate_series

_TWELVEDATA_INTERVALS = {
    Timeframe.M5: "5min",
    Timeframe.M15: "15min",
    Timeframe.M30: "30min",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1day",
}


def _get_json(url: str, timeout: float) -> dict:
    """Fetch and decode JSON, turning every transport failure into a DataError."""
    request = urllib.request.Request(url, headers={"User-Agent": "trading.bot/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DataError(f"provider returned HTTP {exc.code} for {url.split('?')[0]}") from exc
    except urllib.error.URLError as exc:
        raise DataError(f"could not reach provider: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"provider returned malformed JSON: {exc}") from exc


class TwelveDataSource:
    """Twelve Data time-series endpoint.

    Free tier covers major FX pairs at intraday intervals, which is enough to run
    ``scan`` daily. Rate limits are the caller's problem — we surface the API's
    own error message rather than retrying blindly.
    """

    BASE = "https://api.twelvedata.com/time_series"

    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        if not api_key:
            raise DataError(
                "no API key. Set the key in the environment variable named by "
                "data.api_key_env (default TRADING_BOT_API_KEY)."
            )
        self.api_key = api_key
        self.timeout = timeout

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        interval = _TWELVEDATA_INTERVALS.get(timeframe)
        if interval is None:
            raise DataError(f"{timeframe.name} is not supported by Twelve Data")
        pair = symbol.upper()
        formatted = f"{pair[:3]}/{pair[3:6]}" if len(pair) >= 6 and "/" not in pair else pair
        query = urllib.parse.urlencode(
            {
                "symbol": formatted,
                "interval": interval,
                "outputsize": min(max(limit, 1), 5000),
                "apikey": self.api_key,
                "format": "JSON",
                "timezone": "UTC",
            }
        )
        payload = _get_json(f"{self.BASE}?{query}", self.timeout)

        if payload.get("status") == "error" or "values" not in payload:
            raise DataError(
                f"provider error for {formatted}: {payload.get('message', 'no values returned')}"
            )

        candles: list[Candle] = []
        for row in payload["values"]:
            try:
                stamp = datetime.fromisoformat(row["datetime"])
                candles.append(
                    Candle(
                        timestamp=stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0.0),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise DataError(f"malformed bar from provider: {row!r} ({exc})") from exc

        candles.sort(key=lambda c: c.timestamp)
        return validate_series(candles, formatted)


def build_rest_source(provider: str, api_key: str | None, timeout: float = 15.0):
    """Factory so the CLI does not need to know provider class names."""
    name = provider.strip().lower()
    if name in ("twelvedata", "twelve_data", "td"):
        return TwelveDataSource(api_key or "", timeout)
    raise DataError(
        f"unknown data provider {provider!r}. Supported: twelvedata. "
        f"Add a class here implementing DataSource to support another."
    )
