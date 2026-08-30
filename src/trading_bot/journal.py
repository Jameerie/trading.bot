"""Append-only signal journal.

Every signal shown to the user is recorded before it is acted on. The point is
accountability: without a record written at issue time, it is impossible to tell
later whether the system called a move or whether memory is being generous.

The file is JSONL — one signal per line, appended, never rewritten — so a crash
mid-write costs at most the last line, and the history cannot be quietly edited
by a later run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import DataError
from .models import Signal


@dataclass(frozen=True)
class JournalEntry:
    """A journalled signal plus any outcome recorded against it later."""

    recorded_at: datetime
    signal: dict
    outcome: str | None = None
    note: str = ""

    @property
    def symbol(self) -> str:
        return self.signal.get("symbol", "?")

    @property
    def issued_at(self) -> str:
        return self.signal.get("issued_at", "")


class Journal:
    """A JSONL file of issued signals."""

    def __init__(self, path: str | Path = "reports/journal.jsonl") -> None:
        self.path = Path(path)

    def record(self, signal: Signal, note: str = "") -> JournalEntry:
        """Append one signal. Creates the parent directory if needed."""
        entry = JournalEntry(
            recorded_at=datetime.now(timezone.utc), signal=signal.to_dict(), note=note
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "recorded_at": entry.recorded_at.isoformat(),
            "signal": entry.signal,
            "outcome": entry.outcome,
            "note": entry.note,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        return entry

    def read(self) -> list[JournalEntry]:
        """Read every entry. A corrupt line is reported, not skipped silently."""
        if not self.path.exists():
            return []
        entries: list[JournalEntry] = []
        with self.path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DataError(f"{self.path} line {line_no} is not valid JSON: {exc}") from exc
                entries.append(
                    JournalEntry(
                        recorded_at=datetime.fromisoformat(raw["recorded_at"]),
                        signal=raw.get("signal", {}),
                        outcome=raw.get("outcome"),
                        note=raw.get("note", ""),
                    )
                )
        return entries

    def summary(self) -> str:
        """Human-readable digest of what has been issued."""
        entries = self.read()
        if not entries:
            return f"No signals journalled yet ({self.path} is empty or absent)."

        by_symbol: dict[str, int] = {}
        for entry in entries:
            by_symbol[entry.symbol] = by_symbol.get(entry.symbol, 0) + 1

        lines = [
            f"Journal: {self.path}",
            f"{len(entries)} signal(s) recorded",
            "",
            f"{'issued (UTC)':<20} {'symbol':<9} {'dir':<5} {'grade':<6} {'R:R':>5}  outcome",
            "-" * 68,
        ]
        for entry in entries[-30:]:
            sig = entry.signal
            issued = entry.issued_at[:16].replace("T", " ")
            lines.append(
                f"{issued:<20} {sig.get('symbol', '?'):<9} "
                f"{sig.get('direction', '?'):<5} {sig.get('grade', '?'):<6} "
                f"{sig.get('risk_reward', 0):>5.1f}  {entry.outcome or 'not recorded'}"
            )
        if len(entries) > 30:
            lines.append(f"... {len(entries) - 30} earlier entries not shown")
        lines += ["", "By symbol: " + ", ".join(f"{k} {v}" for k, v in sorted(by_symbol.items()))]
        return "\n".join(lines)
