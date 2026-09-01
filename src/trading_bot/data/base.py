"""Data source protocol and shared validation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..errors import DataError
from ..models import Candle, Timeframe


@runtime_checkable
class DataSource(Protocol):
    """Anything that can produce an ordered candle history."""

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        """Return up to ``limit`` candles, oldest first, ending at the latest close."""
        ...


def missing_symbols(
    source: DataSource, symbols: Iterable[str], timeframe: Timeframe
) -> list[str]:
    """Which symbols this source has nothing for, asked before any fetch.

    A source that streams from an API cannot answer without spending a request,
    so it does not implement ``missing`` and gets an empty list here; its
    failures stay per-symbol errors, which is what they are. A directory of CSVs
    *can* answer, and answering is what turns sixty identical "no CSV" errors
    into one line naming the command that fixes them.
    """
    finder = getattr(source, "missing", None)
    return list(finder(symbols, timeframe)) if callable(finder) else []


def validate_series(candles: list[Candle], symbol: str = "") -> list[Candle]:
    """Check a series is ordered, unique and non-empty.

    Duplicate or out-of-order timestamps break every indexed indicator and make
    a backtest silently wrong, so they are rejected here at the boundary rather
    than being tolerated downstream.
    """
    label = f" for {symbol}" if symbol else ""
    if not candles:
        raise DataError(f"empty candle series{label}")
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        if cur.timestamp == prev.timestamp:
            raise DataError(f"duplicate timestamp{label} at {cur.timestamp.isoformat()}")
        if cur.timestamp < prev.timestamp:
            raise DataError(
                f"out-of-order candles{label}: {cur.timestamp.isoformat()} "
                f"follows {prev.timestamp.isoformat()}"
            )
    return candles
