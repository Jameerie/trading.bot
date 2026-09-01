"""Append-only signal journal, with outcome recording.

Every signal shown to the user is recorded before it is acted on, and every
closed trade is recorded against it afterwards. The point is accountability:
without a record written at issue time, it is impossible to tell later whether
the system called a move or whether memory is being generous.

The file is JSONL — one event per line, appended, never rewritten. A close is a
*new line* referencing an earlier signal rather than an edit of it, so the
history is tamper-evident: you can add to it, but you cannot quietly change what
was advised. Reading replays the log and folds closes onto their signals.

This is also the only place where *live* results enter the system. Backtest
numbers describe a simulation; these describe what actually happened, and
``live_metrics`` reports them in the same units so the two can be compared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import DataError
from .instruments import get_instrument, pips_between
from .metrics import Metrics, compute_metrics
from .models import Direction, Outcome, Reason, Signal, Timeframe, Trade, utc_now

# Event kinds written to the log.
KIND_SIGNAL = "signal"
KIND_CLOSE = "close"


def signal_id(symbol: str, issued_at: str) -> str:
    """Stable identifier for a signal: symbol plus its issue timestamp.

    A signal is unique per symbol per bar, so this needs no counter and stays
    stable across restarts and file copies.
    """
    return f"{symbol.upper()}@{issued_at}"


@dataclass
class JournalEntry:
    """A journalled signal, plus its outcome once one has been recorded."""

    recorded_at: datetime
    signal: dict
    outcome: str | None = None
    note: str = ""
    exit_price: float | None = None
    closed_at: datetime | None = None
    r_multiple: float | None = None
    # What the model saw when it issued the signal (every check, fired or not,
    # the indicator readings, the prediction it amounted to) and, once closed,
    # what the market did (fill, excursions, the bars from fill to exit). Both
    # are empty on entries written before the ledger existed.
    context: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.signal.get("symbol", "?")

    @property
    def issued_at(self) -> str:
        return self.signal.get("issued_at", "")

    @property
    def entry_id(self) -> str:
        return signal_id(self.symbol, self.issued_at)

    @property
    def is_open(self) -> bool:
        return self.outcome is None

    @property
    def direction(self) -> str:
        return self.signal.get("direction", "?")


def realised_r(signal: dict, exit_price: float) -> float:
    """R actually achieved, from the planned stop distance.

    Measured against the *planned* risk, so a trade closed early at half the
    target reads as roughly +2R on a 4R setup rather than being scored as a win.
    Partial outcomes are the norm in live trading and the journal should show
    them honestly instead of rounding them to win or loss.
    """
    entry = float(signal["entry"])
    stop = float(signal["stop_loss"])
    sign = 1 if signal.get("direction") == Direction.LONG.value else -1
    risk = (entry - stop) * sign
    if risk <= 0:
        raise DataError(
            f"journalled signal has a stop on the wrong side of entry "
            f"({entry} / {stop}); cannot compute R"
        )
    return ((exit_price - entry) * sign) / risk


def classify_outcome(r_value: float) -> Outcome:
    """Map a realised R onto an outcome label."""
    if r_value > 1e-9:
        return Outcome.WIN
    if r_value < -1e-9:
        return Outcome.LOSS
    return Outcome.BREAKEVEN


class Journal:
    """A JSONL log of issued signals and their outcomes."""

    def __init__(self, path: str | Path = "reports/journal.jsonl") -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ writes

    def _append(self, payload: dict) -> None:
        """Append one event. Creates the parent directory if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def record(self, signal: Signal, note: str = "", context: dict | None = None) -> JournalEntry:
        """Append a signal as issued.

        ``context`` is the snapshot of what the model saw — see
        ``ledger.snapshot``. It is written on the same line as the signal so the
        two can never drift apart, and so a later reader can judge not only
        whether the call was right but whether it was right for the reasons
        given.
        """
        entry = JournalEntry(
            recorded_at=utc_now(), signal=signal.to_dict(), note=note, context=context or {}
        )
        payload = {
            "kind": KIND_SIGNAL,
            "recorded_at": entry.recorded_at.isoformat(),
            "signal": entry.signal,
            "outcome": None,
            "note": note,
        }
        if entry.context:
            payload["context"] = entry.context
        self._append(payload)
        return entry

    def already_recorded(self, signal: Signal) -> bool:
        """Whether this exact signal is already in the log.

        A scan run twice on the same bar should not create two entries; the
        journal is a record of advice given, not of times the button was pressed.
        """
        target = signal_id(signal.symbol, signal.issued_at.isoformat())
        return any(entry.entry_id == target for entry in self.read())

    def record_once(
        self, signal: Signal, note: str = "", context: dict | None = None
    ) -> JournalEntry | None:
        """Append a signal unless it is already journalled."""
        if self.already_recorded(signal):
            return None
        return self.record(signal, note, context)

    def close(
        self,
        entry_id: str,
        exit_price: float,
        closed_at: datetime | None = None,
        note: str = "",
        r_multiple: float | None = None,
        detail: dict | None = None,
    ) -> JournalEntry:
        """Record how a journalled signal actually finished.

        Raises if the signal is unknown or already closed — silently accepting a
        second close would let one trade be scored twice.

        ``r_multiple`` overrides the planned-entry arithmetic. The forward
        resolver passes the simulator's own figure, which is measured from the
        cost-adjusted fill and is therefore the *smaller* number on a win and
        the same on a loss; a human closing a trade by hand leaves it unset and
        gets R against the plan. ``detail`` carries what the market did on the
        way — fill, excursions, the bars from fill to exit — for the ledger.
        """
        entries = {entry.entry_id: entry for entry in self.read()}
        entry = entries.get(entry_id)
        if entry is None:
            known = ", ".join(sorted(entries)[:5]) or "none"
            raise DataError(f"no journalled signal with id {entry_id!r}. Known ids: {known}")
        if not entry.is_open:
            raise DataError(
                f"{entry_id} is already closed as {entry.outcome} at {entry.exit_price}"
            )

        r_value = realised_r(entry.signal, exit_price) if r_multiple is None else float(r_multiple)
        outcome = classify_outcome(r_value)
        stamp = closed_at or utc_now()
        if stamp.tzinfo is None:
            raise DataError("closed_at must be timezone-aware UTC")

        payload = {
            "kind": KIND_CLOSE,
            "recorded_at": utc_now().isoformat(),
            "entry_id": entry_id,
            "exit_price": exit_price,
            "closed_at": stamp.isoformat(),
            "outcome": outcome.value,
            "r_multiple": round(r_value, 4),
            "note": note,
        }
        if detail:
            payload["detail"] = detail
        self._append(payload)
        entry.outcome = outcome.value
        entry.exit_price = exit_price
        entry.closed_at = stamp
        entry.r_multiple = round(r_value, 4)
        entry.detail = dict(detail or {})
        if note:
            entry.note = note
        return entry

    # ------------------------------------------------------------------- reads

    def read(self) -> list[JournalEntry]:
        """Replay the log, folding close events onto their signals.

        A corrupt line is reported rather than skipped: silently dropping part of
        a performance record is worse than refusing to read it.
        """
        if not self.path.exists():
            return []

        entries: list[JournalEntry] = []
        by_id: dict[str, JournalEntry] = {}
        pending_closes: list[dict] = []

        with self.path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DataError(
                        f"{self.path} line {line_no} is not valid JSON: {exc}"
                    ) from exc

                kind = raw.get("kind", KIND_SIGNAL)
                if kind == KIND_CLOSE:
                    pending_closes.append(raw)
                    continue

                entry = JournalEntry(
                    recorded_at=datetime.fromisoformat(raw["recorded_at"]),
                    signal=raw.get("signal", {}),
                    outcome=raw.get("outcome"),
                    note=raw.get("note", ""),
                    context=raw.get("context") or {},
                )
                entries.append(entry)
                by_id[entry.entry_id] = entry

        # Applied after the full pass so a close may appear before its signal in
        # a concatenated or out-of-order file.
        for raw in pending_closes:
            entry = by_id.get(raw.get("entry_id", ""))
            if entry is None:
                continue
            entry.outcome = raw.get("outcome")
            entry.exit_price = raw.get("exit_price")
            entry.r_multiple = raw.get("r_multiple")
            closed = raw.get("closed_at")
            entry.closed_at = datetime.fromisoformat(closed) if closed else None
            entry.detail = raw.get("detail") or {}
            if raw.get("note"):
                entry.note = raw["note"]

        return entries

    def open_entries(self) -> list[JournalEntry]:
        return [entry for entry in self.read() if entry.is_open]

    def closed_entries(self) -> list[JournalEntry]:
        return [entry for entry in self.read() if not entry.is_open]

    # ----------------------------------------------------------------- reports

    def live_metrics(self, confidence: float = 0.95) -> Metrics:
        """Performance of the closed trades, in the same units as a backtest.

        Reusing ``compute_metrics`` means live and simulated results are measured
        identically — including the confidence interval, which matters more here
        because a live sample is always small.
        """
        trades: list[Trade] = []
        for entry in self.closed_entries():
            data = entry.signal
            try:
                signal = _signal_from_dict(data)
            except (KeyError, ValueError, DataError):
                continue
            r_value = entry.r_multiple
            if r_value is None:
                continue
            trades.append(
                Trade(
                    signal=signal,
                    entry_time=signal.issued_at,
                    exit_time=entry.closed_at,
                    exit_price=entry.exit_price,
                    outcome=classify_outcome(r_value),
                    r_multiple=r_value,
                    bars_held=0,
                )
            )
        return compute_metrics(trades, confidence)

    def summary(self) -> str:
        """Human-readable digest of what has been advised and what it did."""
        entries = self.read()
        if not entries:
            return f"No signals journalled yet ({self.path} is empty or absent)."

        closed = [e for e in entries if not e.is_open]
        open_ones = [e for e in entries if e.is_open]

        lines = [
            f"Journal: {self.path}",
            f"{len(entries)} signal(s) recorded - {len(closed)} closed, {len(open_ones)} open",
            "",
            f"{'issued (UTC)':<18} {'symbol':<9} {'dir':<5} {'grade':<6} {'R:R':>5} "
            f"{'outcome':<11} {'R':>7}",
            "-" * 74,
        ]
        for entry in entries[-30:]:
            sig = entry.signal
            issued = entry.issued_at[:16].replace("T", " ")
            r_text = f"{entry.r_multiple:+.2f}" if entry.r_multiple is not None else "-"
            lines.append(
                f"{issued:<18} {sig.get('symbol', '?'):<9} {sig.get('direction', '?'):<5} "
                f"{sig.get('grade', '?'):<6} {sig.get('risk_reward', 0):>5.1f} "
                f"{entry.outcome or 'open':<11} {r_text:>7}"
            )
        if len(entries) > 30:
            lines.append(f"... {len(entries) - 30} earlier entries not shown")

        if closed:
            metrics = self.live_metrics()
            interval = metrics.win_rate_interval
            lines += [
                "",
                "LIVE PERFORMANCE (closed trades only)",
                f"  Trades        {metrics.trades}",
                f"  Win rate      {metrics.win_rate:.1%}  "
                f"(95% interval {interval.low:.1%}-{interval.high:.1%})",
                f"  Expectancy    {metrics.expectancy_r:+.2f}R per trade",
                f"  Total         {metrics.total_r:+.1f}R",
            ]
            if metrics.trades < 30:
                lines.append(
                    f"  {metrics.trades} closed trade(s) is too few to judge. "
                    f"The interval above is the honest width of what you know."
                )
        else:
            lines += [
                "",
                "No closed trades yet. Record outcomes with:",
                "  trading-bot journal --close <id> --exit <price>",
            ]
        return "\n".join(lines)


def _signal_from_dict(data: dict) -> Signal:
    """Rebuild a Signal from its journalled form, for metric computation.

    Reasons and warnings come back too, so a card or a ledger entry rebuilt
    from the journal reads exactly as it did when it was issued.
    """
    return Signal(
        symbol=data["symbol"],
        timeframe=Timeframe.parse(data.get("timeframe", "H1")),
        direction=Direction(data["direction"]),
        entry=float(data["entry"]),
        stop_loss=float(data["stop_loss"]),
        take_profit=float(data["take_profit"]),
        issued_at=datetime.fromisoformat(data["issued_at"]),
        score=float(data.get("score", 0.0)),
        max_score=float(data.get("max_score", 1.0)),
        risk_reward=float(data.get("risk_reward", 0.0)),
        risk_pips=float(data.get("risk_pips", 0.0)),
        reward_pips=float(data.get("reward_pips", 0.0)),
        position_lots=float(data.get("position_lots", 0.0)),
        position_units=float(data.get("position_units", 0.0)),
        risk_amount=float(data.get("risk_amount", 0.0)),
        account_currency=str(data.get("account_currency", "USD")),
        reasons=tuple(
            Reason(str(r.get("code", "?")), str(r.get("detail", "")), float(r.get("weight", 0.0)))
            for r in data.get("reasons", []) or []
            if isinstance(r, dict)
        ),
        warnings=tuple(str(w) for w in data.get("warnings", []) or []),
        strategy=data.get("strategy", "unknown"),
    )
