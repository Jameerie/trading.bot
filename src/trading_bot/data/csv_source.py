"""OHLCV CSV loading, writing, and filling a directory of them.

Accepts the common column spellings that broker and vendor exports use, because
requiring one exact header is a needless obstacle between a user and their own
data. The other half of the module answers the opposite question — which symbols
have no file yet, and how to get one, because "no CSV for EURNOK" repeated
sixty times is a worse answer than one line saying what to run.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..errors import DataError, TradingBotError
from ..models import Candle, Timeframe
from .base import DataSource, validate_series

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

    def candidates(self, symbol: str, timeframe: Timeframe) -> list[Path]:
        """Every filename accepted for this symbol, canonical spelling first."""
        return [
            self.directory / f"{symbol.upper()}_{timeframe.name}.csv",
            self.directory / f"{symbol.upper()}{timeframe.name}.csv",
            self.directory / f"{symbol.lower()}_{timeframe.name.lower()}.csv",
        ]

    def path_for(self, symbol: str, timeframe: Timeframe) -> Path | None:
        """The file backing this symbol, or ``None`` if there isn't one yet."""
        for candidate in self.candidates(symbol, timeframe):
            if candidate.exists():
                return candidate
        return None

    def missing(self, symbols: Iterable[str], timeframe: Timeframe) -> list[str]:
        """Which of ``symbols`` have no file here.

        Asking up front is what lets a caller say "58 pairs have no data, here
        is the one command that fixes it" instead of raising the same error
        fifty-eight times. Order is preserved so the answer is deterministic.
        """
        return [s for s in symbols if self.path_for(s, timeframe) is None]

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        found = self.path_for(symbol, timeframe)
        if found is not None:
            return load_csv(found, limit)
        names = ", ".join(c.name for c in self.candidates(symbol, timeframe))
        raise DataError(
            f"no CSV for {symbol} {timeframe.name} in {self.directory}. "
            f"Looked for: {names}. {remedy(symbol, timeframe)}"
        )


def fill_commands(
    timeframe: Timeframe | None = None,
    symbol: str | None = None,
    only_missing: bool = False,
) -> list[tuple[str, str]]:
    """The two ways to fill a missing file, as (command, what it gives you).

    One definition, because the terminal, the web UI and the exception text all
    hand out this advice and advice that drifts between them is worse than none.
    The order is deliberate: market data first, synthetic second, because only
    one of them can tell you anything about a market.
    """
    scope = f" --symbols {symbol.upper()}" if symbol else ""
    frame = f" --timeframe {timeframe.name}" if timeframe else ""
    only = " --only-missing" if only_missing else ""
    return [
        (
            f"python -m trading_bot data --fetch{only}{scope}{frame}",
            "market data from your provider; needs a provider key",
        ),
        (
            f"python -m trading_bot data --generate{only}{scope}{frame}",
            "synthetic bars; they test the pipeline and are not a market",
        ),
    ]


def remedy(symbol: str | None = None, timeframe: Timeframe | None = None) -> str:
    """What to actually do about a missing file.

    An error that names the file it wanted and stops there leaves the reader to
    guess whether the fix is a download, a config change or a bug report. Both
    real answers are one command, so print them.
    """
    fetch, generate = (command for command, _ in fill_commands(timeframe, symbol))
    return (
        f"Fill it with market data: `{fetch}` (needs a provider key), or with "
        f"synthetic bars for testing the pipeline only: `{generate}`. "
        f"A broker export copied into the directory works too."
    )


@dataclass(frozen=True)
class FillResult:
    """What happened to one symbol while filling a directory."""

    symbol: str
    status: str  # "written" | "skipped" | "failed"
    path: Path | None = None
    bars: int = 0
    message: str = ""


def fill_directory(
    source: DataSource,
    symbols: Iterable[str],
    timeframe: Timeframe,
    directory: str | Path,
    bars: int,
    only_missing: bool = False,
    pause: float = 0.0,
) -> Iterator[FillResult]:
    """Write one CSV per symbol from ``source``, yielding the outcome of each.

    A generator rather than a function returning a list, so a caller filling
    sixty pairs can print each one as it lands instead of going quiet for eight
    minutes.

    ``pause`` waits that many seconds *before* every request after the first.
    Before, rather than after, so a rate-limited provider never sees a burst;
    and a skipped symbol costs no request, so it waits for nothing.

    One symbol failing is never allowed to abandon the rest: a pair the provider
    does not carry is reported and the loop moves on, because the sixty that do
    work are the point of the run.
    """
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    store = CsvSource(out)
    requested = 0

    for symbol in symbols:
        existing = store.path_for(symbol, timeframe)
        if only_missing and existing is not None:
            yield FillResult(symbol, "skipped", existing, message="already on disk")
            continue
        if pause > 0 and requested:
            time.sleep(pause)
        requested += 1
        try:
            candles = source.fetch(symbol, timeframe, bars)
        except TradingBotError as exc:
            yield FillResult(symbol, "failed", message=str(exc))
            continue
        # Write to the canonical spelling, but overwrite the file already backing
        # this symbol if it uses one of the other accepted names — otherwise a
        # refresh silently leaves the stale file in front of the new one.
        path = existing if existing is not None else store.candidates(symbol, timeframe)[0]
        write_csv(path, candles)
        yield FillResult(symbol, "written", path, bars=len(candles))
