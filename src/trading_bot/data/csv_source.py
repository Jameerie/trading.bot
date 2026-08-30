"""OHLCV CSV loading.

Accepts the common column spellings that broker and vendor exports use, because
requiring one exact header is a needless obstacle between a user and their own
data.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from ..errors import DataError
from ..models import Candle, Timeframe
from .base import validate_series

# Accepted header spellings, lowercased, mapped to our canonical field names.
_ALIASES = {
    "timestamp": "timestamp", "time": "timestamp", "date": "timestamp",
    "datetime": "timestamp", "date_time": "timestamp", "gmt time": "timestamp",
    "open": "open", "o": "open", "<open>": "open",
    "high": "high", "h": "high", "<high>": "high",
    "low": "low", "l": "low", "<low>": "low",
    "close": "close", "c": "close", "<close>": "close", "price": "close",
    "volume": "volume", "vol": "volume", "v": "volume", "<vol>": "volume",
    "tickvol": "volume", "tick_volume": "volume",
}

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%m/%d/%Y %H:%M",
)


def parse_timestamp(raw: str) -> datetime:
    """Parse a timestamp into aware UTC, trying ISO then common vendor formats."""
    text = raw.strip().replace("Z", "+00:00")
    if not text:
        raise DataError("empty timestamp")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # Some exports append a timezone suffix like "GMT+0000"; drop it before retrying.
    cleaned = text.split(" GMT")[0].strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Epoch seconds or milliseconds.
    try:
        number = float(cleaned)
        if number > 1e11:
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (ValueError, OSError, OverflowError) as exc:
        raise DataError(f"unrecognised timestamp format: {raw!r}") from exc


def load_csv(path: str | Path, limit: int | None = None) -> list[Candle]:
    """Read an OHLCV CSV into validated candles, oldest first."""
    p = Path(path)
    if not p.exists():
        raise DataError(f"CSV not found: {p}")
    with p.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise DataError(f"{p} is empty") from exc

        mapping: dict[str, int] = {}
        for idx, name in enumerate(header):
            canon = _ALIASES.get(name.strip().lower())
            if canon and canon not in mapping:
                mapping[canon] = idx
        missing = {"timestamp", "open", "high", "low", "close"} - set(mapping)
        if missing:
            raise DataError(
                f"{p} is missing column(s): {', '.join(sorted(missing))}. "
                f"Found header: {', '.join(header)}"
            )

        candles: list[Candle] = []
        for line_no, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            try:
                candles.append(
                    Candle(
                        timestamp=parse_timestamp(row[mapping["timestamp"]]),
                        open=float(row[mapping["open"]]),
                        high=float(row[mapping["high"]]),
                        low=float(row[mapping["low"]]),
                        close=float(row[mapping["close"]]),
                        volume=(
                            float(row[mapping["volume"]])
                            if "volume" in mapping and row[mapping["volume"]].strip()
                            else 0.0
                        ),
                    )
                )
            except (ValueError, IndexError) as exc:
                raise DataError(f"{p} line {line_no}: {exc}") from exc
            except DataError as exc:
                raise DataError(f"{p} line {line_no}: {exc}") from exc

    candles.sort(key=lambda c: c.timestamp)
    validate_series(candles, p.name)
    return candles[-limit:] if limit else candles


def write_csv(path: str | Path, candles: list[Candle]) -> None:
    """Write candles back out in our canonical header form."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow(
                [
                    c.timestamp.isoformat(),
                    f"{c.open:.6f}", f"{c.high:.6f}",
                    f"{c.low:.6f}", f"{c.close:.6f}",
                    f"{c.volume:.2f}",
                ]
            )


class CsvSource:
    """DataSource backed by ``<dir>/<SYMBOL>_<TF>.csv``."""

    def __init__(self, directory: str | Path = "data/samples") -> None:
        self.directory = Path(directory)

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        candidates = [
            self.directory / f"{symbol.upper()}_{timeframe.name}.csv",
            self.directory / f"{symbol.upper()}{timeframe.name}.csv",
            self.directory / f"{symbol.lower()}_{timeframe.name.lower()}.csv",
        ]
        for candidate in candidates:
            if candidate.exists():
                return load_csv(candidate, limit)
        raise DataError(
            f"no CSV for {symbol} {timeframe.name} in {self.directory}. "
            f"Looked for: {', '.join(c.name for c in candidates)}"
        )
