"""The prediction ledger: every call the model made, and what happened next.

A signal card says what to do. This module is the other half of that
conversation — the part where the tool is held to what it said. For every
prediction it keeps a **case file**: what the model saw when it spoke (all
twelve checks, fired or not, and the readings behind them), the claim it made,
the deadline it set, the odds it quoted, and then, once the market has
answered, what actually happened bar by bar — where it filled, how far it went
the right way, how far the wrong way, which level it reached, and the R and the
cash that produced.

Two ledgers come out of the same machinery and are never mixed:

* The **forward** ledger is built from the journal. Every entry in it was
  written down before its outcome existed, and settled later by the same
  resolver the backtest uses. That is the only track record this tool will
  ever claim.
* A **replay** ledger walks a history and shows what the model would have said
  at every bar and what followed. It is a backtest wearing a diary. It is
  useful — it shows the *shape* of the model's calls, which checks travel with
  the winners, whether the confidence number means anything — and it is
  labelled on every line, because the outcomes were in the file before the
  calls were made.

The scorecards read only resolved cases and report every rate with its Wilson
interval and its sample size, for the reasons ``metrics`` sets out. A
calibration table asks the question a confidence number invites: when the
model said 80%, was it right more often than when it said 72%? A check table
asks which reasons actually travelled with the winners. Neither changes what
the model does; both tell the human how far to believe it.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .backtest import run_backtest, simulate_trade
from .clock import Clock, humanise_delta
from .config import Config
from .forecast import (
    BaseRate,
    Prediction,
    bars_to_time,
    build_prediction,
    empty_base_rate,
)
from .instruments import Instrument, get_instrument, pips_between
from .journal import Journal, JournalEntry, _signal_from_dict, signal_id
from .metrics import Edge, Interval, Metrics, compute_metrics, measure_edge, wilson_interval
from .models import Candle, Direction, Outcome, Signal, Timeframe, Trade, utc_now
from .playbook import CHECK_GUIDE, wrap
from .scanner import Evaluation
from .sessions import session_label
from .strategy import get_strategy

ORIGIN_FORWARD = "forward"
ORIGIN_REPLAY = "replay"

# Bars of price path stored with a settled prediction. The horizon is 200 bars
# by default, so this keeps the whole run of a trade that went the distance.
MAX_PATH_BARS = 250

# Confidence bands for the calibration table. The shipped threshold is 70%, so
# the first band is where most signals land and the last is the rare A+ call.
CONFIDENCE_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.70, "below 70%"),
    (0.70, 0.75, "70-74%"),
    (0.75, 0.80, "75-79%"),
    (0.80, 0.85, "80-84%"),
    (0.85, 1.01, "85%+"),
)

SNAPSHOT_VERSION = 1


# ------------------------------------------------------------- case files


@dataclass(frozen=True)
class CheckRow:
    """One confluence check as the model saw it: fired or not, and why."""

    code: str
    title: str
    weight: float
    fired: bool
    detail: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "weight": self.weight,
            "fired": self.fired,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PathBar:
    """One bar of what the market did after the fill."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

    def to_dict(self) -> dict:
        return {
            "t": self.timestamp.isoformat(),
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
        }


@dataclass(frozen=True)
class Result:
    """What the market did with the prediction, once it had answered."""

    outcome: Outcome
    r_multiple: float
    exit_price: float | None
    exit_time: datetime | None
    fill_price: float | None = None
    fill_time: datetime | None = None
    bars_held: int | None = None
    mae_r: float | None = None
    mfe_r: float | None = None
    note: str = ""
    path: tuple[PathBar, ...] = ()
    # "fill with costs" when the simulator settled it, "planned entry" when a
    # human closed it by hand and R was measured against the plan.
    r_basis: str = "fill with costs"

    @property
    def is_win(self) -> bool:
        return self.r_multiple > 1e-9

    @property
    def is_loss(self) -> bool:
        return self.r_multiple < -1e-9

    @property
    def verdict(self) -> str:
        """RIGHT, WRONG, or neither — the word a person wants first."""
        if self.outcome is Outcome.WIN:
            return "RIGHT"
        if self.outcome is Outcome.LOSS:
            return "WRONG"
        if self.outcome is Outcome.EXPIRED:
            return "EXPIRED"
        return "RIGHT" if self.is_win else "WRONG" if self.is_loss else "FLAT"

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "verdict": self.verdict,
            "r_multiple": round(self.r_multiple, 4),
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "fill_price": self.fill_price,
            "fill_time": self.fill_time.isoformat() if self.fill_time else None,
            "bars_held": self.bars_held,
            "mae_r": self.mae_r,
            "mfe_r": self.mfe_r,
            "note": self.note,
            "r_basis": self.r_basis,
            "path": [bar.to_dict() for bar in self.path],
        }


@dataclass(frozen=True)
class CaseFile:
    """One prediction, everything the model saw, and everything that followed."""

    id: str
    origin: str
    signal: Signal
    checks: tuple[CheckRow, ...]
    readings: dict
    session: str
    prediction: Prediction | None
    result: Result | None
    recorded_at: datetime | None = None
    note: str = ""
    # False when the journal entry predates the ledger, so the checks that did
    # not fire were reconstructed from the strategy's table rather than recorded.
    snapshot_complete: bool = True

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def direction(self) -> Direction:
        return self.signal.direction

    @property
    def made_at(self) -> datetime:
        return self.signal.issued_at

    @property
    def is_open(self) -> bool:
        return self.result is None

    @property
    def is_resolved(self) -> bool:
        return self.result is not None

    @property
    def verdict(self) -> str:
        return "OPEN" if self.result is None else self.result.verdict

    @property
    def r_multiple(self) -> float | None:
        return None if self.result is None else self.result.r_multiple

    @property
    def cash(self) -> float | None:
        """P&L in account currency at the size the card recommended."""
        if self.result is None:
            return None
        return round(self.result.r_multiple * self.signal.risk_amount, 2)

    @property
    def fired(self) -> tuple[CheckRow, ...]:
        return tuple(c for c in self.checks if c.fired)

    @property
    def missing(self) -> tuple[CheckRow, ...]:
        return tuple(c for c in self.checks if not c.fired)

    def as_trade(self) -> Trade | None:
        """The resolved case in the shape ``metrics`` understands."""
        if self.result is None:
            return None
        r = self.result
        return Trade(
            signal=self.signal,
            entry_time=r.fill_time or self.signal.issued_at,
            exit_time=r.exit_time,
            exit_price=r.exit_price,
            outcome=r.outcome,
            r_multiple=r.r_multiple,
            bars_held=r.bars_held or 0,
            mae_r=r.mae_r or 0.0,
            mfe_r=r.mfe_r or 0.0,
            fill_price=r.fill_price,
        )

    def to_dict(self, clock: Clock | None = None) -> dict:
        data = {
            "id": self.id,
            "origin": self.origin,
            "signal": self.signal.to_dict(),
            "checks": [c.to_dict() for c in self.checks],
            "readings": dict(self.readings),
            "session": self.session,
            "prediction": self.prediction.to_dict() if self.prediction else None,
            "result": self.result.to_dict() if self.result else None,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "note": self.note,
            "snapshot_complete": self.snapshot_complete,
            "verdict": self.verdict,
            "cash": self.cash,
            "is_open": self.is_open,
        }
        instrument = get_instrument(self.symbol)
        data["digits"] = instrument.digits
        data["description"] = instrument.describe()
        if clock is not None:
            data["made_at_local"] = clock.stamp(self.made_at)
            if self.prediction is not None:
                data["prediction"]["entry_deadline_local"] = clock.stamp(
                    self.prediction.entry_deadline
                )
                data["prediction"]["resolve_by_local"] = clock.stamp(self.prediction.resolve_by)
            if self.result is not None:
                if self.result.exit_time:
                    data["result"]["exit_time_local"] = clock.stamp(self.result.exit_time)
                if self.result.fill_time:
                    data["result"]["fill_time_local"] = clock.stamp(self.result.fill_time)
                data["result"]["narrative"] = describe_result(self, instrument, clock)
        return data


# --------------------------------------------------------------- snapshots


def _check_table(config: Config) -> list[tuple[str, float]]:
    """Every check the configured strategy runs, in its own order, with weights."""
    strategy = get_strategy(config.strategy.name)
    engine = getattr(strategy, "engine", None)
    if engine is None:
        return []
    return [(check.code, check.weight) for check in engine.checks]


def _readings(evaluation: Evaluation) -> dict:
    """The numbers behind the checks, rounded for a record, not for a decision."""
    ctx = evaluation.context
    if ctx is None:
        return {}

    def r(value, digits=5):
        return None if value is None else round(float(value), digits)

    candle = ctx.candle
    last_break = ctx.view.last_break
    high = ctx.view.last_swing_high
    low = ctx.view.last_swing_low
    return {
        "price": r(ctx.price),
        "bar_open": r(candle.open),
        "bar_high": r(candle.high),
        "bar_low": r(candle.low),
        "bar_body_ratio": r(candle.body_ratio, 3),
        "atr": r(ctx.atr, 6),
        "rsi": r(ctx.rsi, 2),
        "adx": r(ctx.adx, 2),
        "plus_di": r(ctx.plus_di, 2),
        "minus_di": r(ctx.minus_di, 2),
        "ema_fast": r(ctx.ema_fast),
        "ema_slow": r(ctx.ema_slow),
        "ema_trend": r(ctx.ema_trend),
        "ema_fast_slope": r(ctx.ema_fast_slope, 7),
        "htf_trend": ctx.htf_trend.value,
        "trend": ctx.view.trend.value,
        "last_break": None if last_break is None else {
            "kind": last_break.kind,
            "direction": last_break.direction.value,
            "level": r(last_break.level),
            "bars_ago": ctx.index - last_break.index,
        },
        "swing_high": None if high is None else r(high.price),
        "swing_low": None if low is None else r(low.price),
        "swings_known": len(ctx.view.swings),
    }


def snapshot(evaluation: Evaluation, config: Config, prediction: Prediction | None = None) -> dict:
    """Everything the model saw when it issued this signal, as a plain dict.

    Written into the journal beside the signal, so a later reader can judge not
    only whether the call was right but whether it was right for the reasons
    given. Every check is listed — the ones that fired with their detail, the
    ones that did not by name — because a ledger that records only the reasons
    *for* a trade cannot later ask which reasons mattered.
    """
    scored = evaluation.confluence
    fired = {r.code: r for r in (scored.reasons if scored else ())}
    if not fired and evaluation.signal is not None:
        fired = {r.code: r for r in evaluation.signal.reasons}

    checks = []
    for code, weight in _check_table(config):
        reason = fired.get(code)
        checks.append({
            "code": code,
            "weight": weight,
            "fired": reason is not None,
            "detail": reason.detail if reason is not None else "",
        })

    stamp = evaluation.context.timestamp if evaluation.context else None
    return {
        "version": SNAPSHOT_VERSION,
        "checks": checks,
        "readings": _readings(evaluation),
        "session": session_label(stamp) if stamp else "",
        "prediction": prediction.to_dict() if prediction is not None else None,
    }


def outcome_detail(trade: Trade, candles: list[Candle], entry_index: int) -> dict:
    """What the market did, in the form the journal stores with a close.

    The path runs from the fill bar to the exit bar so the ledger can draw the
    whole trade later without asking the provider for bars it may no longer
    serve. Timestamps are epoch seconds to keep the line short.
    """
    last = min(entry_index + trade.bars_held, len(candles) - 1)
    path = [
        [int(c.timestamp.timestamp()), c.open, c.high, c.low, c.close]
        for c in candles[entry_index : last + 1][:MAX_PATH_BARS]
    ]
    return {
        "fill_price": trade.fill_price,
        "fill_time": trade.entry_time.isoformat(),
        "exit_price": trade.exit_price,
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "outcome": trade.outcome.value,
        "r_multiple": round(trade.r_multiple, 4),
        "bars_held": trade.bars_held,
        "mae_r": trade.mae_r,
        "mfe_r": trade.mfe_r,
        "r_basis": "fill with costs",
        "path": path,
    }


def _decode_path(raw) -> tuple[PathBar, ...]:
    bars: list[PathBar] = []
    for item in raw or []:
        try:
            stamp, o, h, l, c = item[:5]
            bars.append(PathBar(
                datetime.fromtimestamp(int(stamp), tz=timezone.utc),
                float(o), float(h), float(l), float(c),
            ))
        except (TypeError, ValueError, IndexError):
            continue
    return tuple(bars)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _checks_from_snapshot(rows, config: Config) -> tuple[CheckRow, ...]:
    out = []
    for row in rows:
        code = str(row.get("code", "?"))
        title, need = CHECK_GUIDE.get(code, (code, "no description available"))
        fired = bool(row.get("fired"))
        out.append(CheckRow(
            code=code,
            title=title,
            weight=float(row.get("weight", 0.0)),
            fired=fired,
            detail=str(row.get("detail") or "") if fired else need,
        ))
    return tuple(out)


def _checks_from_signal(signal: Signal, config: Config) -> tuple[CheckRow, ...]:
    """Reconstruct the check list for an entry recorded before snapshots existed.

    The reasons that fired travel with the signal; the ones that did not are
    inferred from the strategy's table. The case is flagged as reconstructed.
    """
    fired = {r.code: r for r in signal.reasons}
    rows = []
    for code, weight in _check_table(config):
        reason = fired.get(code)
        rows.append({
            "code": code,
            "weight": reason.weight if reason else weight,
            "fired": reason is not None,
            "detail": reason.detail if reason else "",
        })
    return _checks_from_snapshot(rows, config)


def _base_rate_from_dict(symbol: str, data: dict | None, config: Config) -> BaseRate:
    if not data:
        return empty_base_rate(symbol, "not measured", config.target.confidence)
    try:
        return BaseRate(
            symbol=str(data.get("symbol", symbol)).upper(),
            sample=int(data.get("sample", 0)),
            win_rate=float(data.get("win_rate", 0.0)),
            interval=Interval(
                float(data.get("interval_low", 0.0)),
                float(data.get("interval_high", 1.0)),
                config.target.confidence,
            ),
            expectancy_r=float(data.get("expectancy_r", 0.0)),
            average_rr=float(data.get("average_rr", 0.0)),
            source=str(data.get("source", "not measured")),
            out_of_sample=bool(data.get("out_of_sample", False)),
        )
    except (TypeError, ValueError):
        return empty_base_rate(symbol, "not measured", config.target.confidence)


def _prediction_from_dict(signal: Signal, data: dict | None, config: Config) -> Prediction:
    """Rebuild the prediction as it was made, deadlines included.

    The deadlines are read back rather than recomputed so that a later change
    to ``backtest.max_bars_in_trade`` cannot quietly move the goalposts on a
    claim that was already on record.
    """
    if not data:
        return build_prediction(signal, config)
    entry_deadline = _parse_time(data.get("entry_deadline"))
    resolve_by = _parse_time(data.get("resolve_by"))
    if entry_deadline is None or resolve_by is None:
        return build_prediction(
            signal, config, _base_rate_from_dict(signal.symbol, data.get("base_rate"), config)
        )
    return Prediction(
        signal=signal,
        made_at=_parse_time(data.get("made_at")) or signal.issued_at,
        entry_deadline=entry_deadline,
        resolve_by=resolve_by,
        horizon_bars=int(data.get("horizon_bars", config.backtest.max_bars_in_trade)),
        entry_window_bars=int(data.get("entry_window_bars", config.backtest.entry_expiry_bars)),
        base_rate=_base_rate_from_dict(signal.symbol, data.get("base_rate"), config),
    )


def case_from_entry(entry: JournalEntry, config: Config) -> CaseFile:
    """Build the case file for one journalled prediction."""
    signal = _signal_from_dict(entry.signal)
    context = entry.context or {}
    complete = bool(context.get("checks"))
    checks = (
        _checks_from_snapshot(context["checks"], config)
        if complete
        else _checks_from_signal(signal, config)
    )
    session = str(context.get("session") or session_label(signal.issued_at))
    prediction = _prediction_from_dict(signal, context.get("prediction"), config)

    result = None
    if not entry.is_open and entry.r_multiple is not None:
        detail = entry.detail or {}
        try:
            outcome = Outcome(entry.outcome)
        except ValueError:
            outcome = Outcome.WIN if entry.r_multiple > 0 else Outcome.LOSS
        result = Result(
            outcome=outcome,
            r_multiple=float(entry.r_multiple),
            exit_price=entry.exit_price,
            exit_time=entry.closed_at,
            fill_price=detail.get("fill_price"),
            fill_time=_parse_time(detail.get("fill_time")),
            bars_held=detail.get("bars_held"),
            mae_r=detail.get("mae_r"),
            mfe_r=detail.get("mfe_r"),
            note=entry.note,
            path=_decode_path(detail.get("path")),
            r_basis=str(detail.get("r_basis") or "planned entry"),
        )

    return CaseFile(
        id=entry.entry_id,
        origin=ORIGIN_FORWARD,
        signal=signal,
        checks=checks,
        readings=dict(context.get("readings") or {}),
        session=session,
        prediction=prediction,
        result=result,
        recorded_at=entry.recorded_at,
        note=entry.note,
        snapshot_complete=complete,
    )


def case_from_replay(
    evaluation: Evaluation, trade: Trade, candles: list[Candle], config: Config
) -> CaseFile:
    """Build the case file for one replayed trade, labelled as such."""
    signal = trade.signal
    snap = snapshot(evaluation, config)
    entry_index = evaluation.index + 1
    last = min(entry_index + trade.bars_held, len(candles) - 1)
    path = tuple(
        PathBar(c.timestamp, c.open, c.high, c.low, c.close)
        for c in candles[entry_index : last + 1][:MAX_PATH_BARS]
    )
    result = Result(
        outcome=trade.outcome,
        r_multiple=trade.r_multiple,
        exit_price=trade.exit_price,
        exit_time=trade.exit_time,
        fill_price=trade.fill_price,
        fill_time=trade.entry_time,
        bars_held=trade.bars_held,
        mae_r=trade.mae_r,
        mfe_r=trade.mfe_r,
        note="replayed from history: the outcome was in the file",
        path=path,
        r_basis="fill with costs",
    )
    return CaseFile(
        id=signal_id(signal.symbol, signal.issued_at.isoformat()),
        origin=ORIGIN_REPLAY,
        signal=signal,
        checks=_checks_from_snapshot(snap["checks"], config),
        readings=snap["readings"],
        session=snap["session"],
        prediction=build_prediction(signal, config),
        result=result,
    )


def load_cases(journal: Journal, config: Config) -> list[CaseFile]:
    """Every journalled prediction as a case file, oldest first.

    An unreadable entry is skipped rather than failing the whole ledger; the
    journal's own reader has already refused malformed JSON, so what remains
    here is a schema the ledger does not understand, and one such line should
    not hide the rest of the record.
    """
    cases: list[CaseFile] = []
    for entry in journal.read():
        try:
            cases.append(case_from_entry(entry, config))
        except (KeyError, ValueError, TypeError):
            continue
    cases.sort(key=lambda c: c.made_at)
    return cases


def replay(
    candles: list[Candle],
    symbol: str,
    config: Config,
    start: int | None = None,
    end: int | None = None,
    strategy=None,
) -> list[CaseFile]:
    """What the model would have said at every bar of ``candles``, and what followed.

    Exactly the backtester's walk — one position at a time, fills at the next
    open, ties against us — with the case file kept for every trade. Nothing
    here touches the journal: a replay cannot put an entry on the forward
    record, and ``tests/test_ledger.py`` asserts that it never does.
    """
    result = run_backtest(
        candles, symbol, config, start=start, end=end, label="replay",
        strategy=strategy, keep_evaluations=True,
    )
    return [
        case_from_replay(evaluation, trade, candles, config)
        for evaluation, trade in zip(result.evaluations, result.trades)
    ]


# --------------------------------------------------------------- scorecards


@dataclass(frozen=True)
class Bucket:
    """Resolved cases sharing a label, measured the way every rate here is."""

    label: str
    trades: int
    wins: int
    losses: int
    expired: int
    win_rate: float
    interval: Interval
    expectancy_r: float
    total_r: float
    cash: float

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "expired": self.expired,
            "win_rate": round(self.win_rate, 4),
            "interval_low": round(self.interval.low, 4),
            "interval_high": round(self.interval.high, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "total_r": round(self.total_r, 3),
            "cash": round(self.cash, 2),
        }


def bucket(label: str, cases: list[CaseFile], confidence: float = 0.95) -> Bucket:
    resolved = [c for c in cases if c.result is not None]
    r_values = [c.result.r_multiple for c in resolved]
    wins = sum(1 for r in r_values if r > 1e-9)
    losses = sum(1 for r in r_values if r < -1e-9)
    expired = sum(1 for c in resolved if c.result.outcome is Outcome.EXPIRED)
    total = sum(r_values)
    n = len(resolved)
    return Bucket(
        label=label,
        trades=n,
        wins=wins,
        losses=losses,
        expired=expired,
        win_rate=(wins / n) if n else 0.0,
        interval=wilson_interval(wins, n, confidence),
        expectancy_r=(total / n) if n else 0.0,
        total_r=total,
        cash=sum(c.cash or 0.0 for c in resolved),
    )


def breakdown(
    cases: list[CaseFile],
    key: Callable[[CaseFile], str | None],
    confidence: float = 0.95,
    order: list[str] | None = None,
) -> list[Bucket]:
    """Group resolved cases by ``key`` and measure each group.

    Groups are ordered by ``order`` when one is given (bands, weekdays), and
    otherwise by sample size, then label — never by win rate, which would put
    the luckiest small group at the top.
    """
    groups: dict[str, list[CaseFile]] = {}
    for case in cases:
        if case.result is None:
            continue
        label = key(case)
        if label is None:
            continue
        groups.setdefault(label, []).append(case)
    if order:
        labels = [label for label in order if label in groups]
        labels += sorted(label for label in groups if label not in order)
    else:
        labels = sorted(groups, key=lambda label: (-len(groups[label]), label))
    return [bucket(label, groups[label], confidence) for label in labels]


def confidence_band(confidence: float) -> str:
    for low, high, label in CONFIDENCE_BANDS:
        if low <= confidence < high:
            return label
    return CONFIDENCE_BANDS[-1][2]


def by_symbol(case: CaseFile) -> str:
    return case.symbol


def by_direction(case: CaseFile) -> str:
    return "buy" if case.direction is Direction.LONG else "sell"


def by_grade(case: CaseFile) -> str:
    return case.signal.grade


def by_band(case: CaseFile) -> str:
    return confidence_band(case.signal.confidence)


def by_session(case: CaseFile) -> str:
    return case.session or session_label(case.made_at)


def by_month(case: CaseFile) -> str:
    return case.made_at.strftime("%Y-%m")


def by_weekday(case: CaseFile) -> str:
    return case.made_at.strftime("%a")


def by_strategy(case: CaseFile) -> str:
    return case.signal.strategy


GRADE_ORDER = ["A+", "A", "B", "C", "D"]
WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BAND_ORDER = [label for _, _, label in CONFIDENCE_BANDS]


def calibration(cases: list[CaseFile], confidence: float = 0.95) -> list[Bucket]:
    """Win rate by the confidence the model quoted.

    This is the table that says whether the number on the card means anything.
    If 80% calls win no more often than 72% calls, confidence is measuring how
    many boxes were ticked, not how likely the trade is to work — and the
    reader should stop weighting by it.
    """
    return breakdown(cases, by_band, confidence, order=BAND_ORDER)


@dataclass(frozen=True)
class CheckEffect:
    """How resolved cases fared with and without one check firing."""

    code: str
    title: str
    weight: float
    fired: Bucket
    missing: Bucket

    @property
    def difference(self) -> float | None:
        """Win rate with the check minus without it, when both sides exist."""
        if self.fired.trades == 0 or self.missing.trades == 0:
            return None
        return self.fired.win_rate - self.missing.win_rate

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "weight": self.weight,
            "fired": self.fired.to_dict(),
            "missing": self.missing.to_dict(),
            "difference": None if self.difference is None else round(self.difference, 4),
        }


def check_attribution(cases: list[CaseFile], confidence: float = 0.95) -> list[CheckEffect]:
    """Which reasons travelled with the winners.

    Every signal passed the confluence threshold, so most checks fired on most
    cases and the "missing" side is thin by construction. The comparison is
    still worth printing: a check whose absence costs nothing is a check whose
    weight is decoration, and a check that fires on every loser is a warning.
    """
    resolved = [c for c in cases if c.result is not None]
    codes: dict[str, tuple[str, float]] = {}
    for case in resolved:
        for row in case.checks:
            codes.setdefault(row.code, (row.title, row.weight))
    effects = []
    for code, (title, weight) in codes.items():
        with_it = [c for c in resolved if any(r.code == code and r.fired for r in c.checks)]
        without = [c for c in resolved if any(r.code == code and not r.fired for r in c.checks)]
        effects.append(CheckEffect(
            code=code,
            title=title,
            weight=weight,
            fired=bucket("fired", with_it, confidence),
            missing=bucket("missing", without, confidence),
        ))
    effects.sort(key=lambda e: -e.weight)
    return effects


@dataclass(frozen=True)
class LedgerSummary:
    """The record in one block: how many calls, how many right, how much."""

    origin: str
    made: int
    resolved: int
    still_open: int
    metrics: Metrics
    edge: Edge
    cash_total: float
    currency: str
    first_made: datetime | None
    last_made: datetime | None
    min_sample: int
    confidence: float

    @property
    def has_verdict(self) -> bool:
        return self.resolved >= self.min_sample

    def lines(self, clock: Clock) -> list[str]:
        """Plain sentences, with the sample size on the same line as every rate."""
        if self.made == 0:
            if self.origin == ORIGIN_REPLAY:
                return ["The model made no calls on this history."]
            return [
                "No predictions on record yet.",
                "Run a scan: every signal it issues is written down before the outcome",
                "is knowable, and this ledger keeps score of them from then on.",
            ]
        m = self.metrics
        interval = m.win_rate_interval
        span = ""
        if self.first_made and self.last_made:
            span = f" between {clock.day(self.first_made)} and {clock.day(self.last_made)}"
        lines = [
            f"{self.made} prediction(s){span}: {self.resolved} resolved, "
            f"{self.still_open} still open",
        ]
        if self.resolved == 0:
            lines.append("Nothing has resolved yet, so there is no win rate to report.")
            return lines
        lines += [
            f"Right {m.wins}, wrong {m.losses}"
            + (f", expired {m.expired}" if m.expired else "")
            + f" -> win rate {m.win_rate:.1%} ({interval.confidence:.0%} interval "
              f"{interval.low:.1%}-{interval.high:.1%}, n={self.resolved})",
            f"Expectancy {m.expectancy_r:+.2f}R per prediction, {m.total_r:+.1f}R in total, "
            f"{self.cash_total:+,.2f} {self.currency} at the sizes the cards gave",
            f"Average win {m.average_win_r:+.2f}R, average loss {m.average_loss_r:+.2f}R, "
            f"worst run {m.max_loss_streak} losses, deepest drawdown {m.max_drawdown_r:.1f}R",
            f"Edge over chance: {self.edge.verdict} - {self.edge.detail}",
        ]
        if not self.has_verdict:
            lines.append(
                f"{self.resolved} resolved prediction(s) is below the {self.min_sample} this "
                f"ledger needs before it will call any of this an edge or a failure."
            )
        return lines

    def to_dict(self, clock: Clock | None = None) -> dict:
        m = self.metrics
        interval = m.win_rate_interval
        return {
            "origin": self.origin,
            "made": self.made,
            "resolved": self.resolved,
            "still_open": self.still_open,
            "wins": m.wins,
            "losses": m.losses,
            "expired": m.expired,
            "win_rate": round(m.win_rate, 4),
            "interval_low": round(interval.low, 4),
            "interval_high": round(interval.high, 4),
            "expectancy_r": round(m.expectancy_r, 4),
            "total_r": round(m.total_r, 3),
            "average_win_r": round(m.average_win_r, 4),
            "average_loss_r": round(m.average_loss_r, 4),
            "max_drawdown_r": round(m.max_drawdown_r, 3),
            "max_loss_streak": m.max_loss_streak,
            "max_win_streak": m.max_win_streak,
            "average_bars_held": round(m.average_bars_held, 1),
            "profit_factor": round(m.profit_factor, 3) if m.losses else None,
            "cash_total": round(self.cash_total, 2),
            "currency": self.currency,
            "edge": {
                "verdict": self.edge.verdict,
                "proven": self.edge.proven,
                "baseline": round(self.edge.baseline, 4),
                "risk_reward": round(self.edge.risk_reward, 3),
                "detail": self.edge.detail,
            },
            "has_verdict": self.has_verdict,
            "min_sample": self.min_sample,
            "first_made": self.first_made.isoformat() if self.first_made else None,
            "last_made": self.last_made.isoformat() if self.last_made else None,
            "lines": self.lines(clock) if clock else [],
        }


def summarise(cases: list[CaseFile], config: Config, origin: str = ORIGIN_FORWARD) -> LedgerSummary:
    """Measure a ledger with the same arithmetic as a backtest, and no more."""
    trades = [t for t in (c.as_trade() for c in cases) if t is not None]
    metrics = compute_metrics(trades, config.target.confidence)
    resolved = [c for c in cases if c.result is not None]
    made_at = [c.made_at for c in cases]
    return LedgerSummary(
        origin=origin,
        made=len(cases),
        resolved=len(resolved),
        still_open=len(cases) - len(resolved),
        metrics=metrics,
        edge=measure_edge(metrics, config.target.min_sample),
        cash_total=sum(c.cash or 0.0 for c in resolved),
        currency=config.account.currency,
        first_made=min(made_at) if made_at else None,
        last_made=max(made_at) if made_at else None,
        min_sample=config.target.min_sample,
        confidence=config.target.confidence,
    )


# ------------------------------------------------------- open predictions


STATE_ENTRY_OPEN = "ENTRY WINDOW OPEN"
STATE_RUNNING = "RUNNING"
STATE_STALE = "STALE"
STATE_RESOLVED = "RESOLVED, NOT YET SETTLED"
STATE_NO_DATA = "NO DATA"
STATE_WAITING = "WAITING FOR A BAR"


@dataclass(frozen=True)
class LiveStatus:
    """Where an open prediction stands right now, and what to do about it."""

    case: CaseFile
    state: str
    bars_since: int
    current_price: float | None
    unrealised_r: float | None
    to_target_pips: float | None
    to_stop_pips: float | None
    mfe_r: float | None
    mae_r: float | None
    entry_window_open: bool
    advice: tuple[str, ...]
    detail: str

    def to_dict(self, clock: Clock | None = None) -> dict:
        data = {
            "id": self.case.id,
            "symbol": self.case.symbol,
            "direction": self.case.direction.value,
            "state": self.state,
            "bars_since": self.bars_since,
            "current_price": self.current_price,
            "unrealised_r": None if self.unrealised_r is None else round(self.unrealised_r, 3),
            "to_target_pips": None if self.to_target_pips is None else round(self.to_target_pips, 1),
            "to_stop_pips": None if self.to_stop_pips is None else round(self.to_stop_pips, 1),
            "mfe_r": self.mfe_r,
            "mae_r": self.mae_r,
            "entry_window_open": self.entry_window_open,
            "advice": list(self.advice),
            "detail": self.detail,
        }
        if clock is not None and self.case.prediction is not None:
            p = self.case.prediction
            data["entry_deadline_local"] = clock.stamp(p.entry_deadline)
            data["resolve_by_local"] = clock.stamp(p.resolve_by)
        return data


def _order_words(signal: Signal, instrument: Instrument) -> str:
    side = "Buy Limit" if signal.direction is Direction.LONG else "Sell Limit"
    return (
        f"{side} {signal.symbol} at {signal.entry:.{instrument.digits}f}, "
        f"{signal.position_lots:.2f} lots, stop {signal.stop_loss:.{instrument.digits}f}, "
        f"target {signal.take_profit:.{instrument.digits}f}"
    )


def live_status(
    case: CaseFile, candles: list[Candle], config: Config, now: datetime | None = None
) -> LiveStatus:
    """Judge an open prediction against the bars that have closed since.

    The advice is deliberately conditional — "if you are in" and "if you are
    not" — because the tool does not know which. It never placed the order, so
    it cannot know whether the human did.
    """
    now = now or utc_now()
    signal = case.signal
    instrument = get_instrument(signal.symbol)
    prediction = case.prediction or build_prediction(signal, config)
    window_open = now <= prediction.entry_deadline
    sign = signal.direction.sign
    ticket = _order_words(signal, instrument)
    deadline_left = humanise_delta(prediction.entry_deadline - now)
    resolve_left = humanise_delta(prediction.resolve_by - now)

    index = next((i for i, c in enumerate(candles) if c.timestamp == signal.issued_at), None)
    if index is None:
        return LiveStatus(
            case, STATE_NO_DATA, 0, None, None, None, None, None, None, window_open,
            (
                "The bar this prediction was made on is not in the data on hand, so it "
                "cannot be judged. Fetch a longer window, or check that the data source "
                "still serves that date.",
            ),
            "decision bar not found in the supplied candles",
        )

    available = len(candles) - index - 1
    if available < 1:
        if window_open:
            advice = (
                f"No bar has closed since the call. The entry window is open for another "
                f"{deadline_left}, until {config.clock.stamp(prediction.entry_deadline)}.",
                f"If you want this trade, the ticket is: {ticket}.",
                "Place the stop loss on the same ticket as the entry. Do not chase with a "
                "market order if the price has already run.",
            )
            state = STATE_ENTRY_OPEN
        else:
            advice = (
                "No bar has closed since the call in the data on hand, but the entry "
                "window has passed on the clock. The data is stale: fetch fresh candles "
                "before deciding anything.",
            )
            state = STATE_WAITING
        return LiveStatus(
            case, state, 0, candles[-1].close, None, None, None, None, None, window_open,
            advice, "no closed bar since the decision bar",
        )

    trade = simulate_trade(candles, signal, index, config, instrument)
    current = candles[-1].close
    fill = trade.fill_price if trade and trade.fill_price is not None else signal.entry
    risk = (fill - signal.stop_loss) * sign
    unrealised = ((current - fill) * sign / risk) if risk > 0 else None
    to_target = pips_between(instrument, current, signal.take_profit)
    to_stop = pips_between(instrument, current, signal.stop_loss)
    horizon = prediction.horizon_bars

    if trade is not None and trade.outcome in (Outcome.WIN, Outcome.LOSS):
        word = "reached the target" if trade.outcome is Outcome.WIN else "hit the stop"
        when = config.clock.stamp(trade.exit_time) if trade.exit_time else "?"
        return LiveStatus(
            case, STATE_RESOLVED, available, current, unrealised, to_target, to_stop,
            trade.mfe_r, trade.mae_r, window_open,
            (
                f"The market has answered: price {word} at {when}, "
                f"{trade.bars_held} bar(s) after the fill, for {trade.r_multiple:+.2f}R.",
                "Settle it onto the record with:  python -m trading_bot ledger --resolve",
                "If you took the trade and your broker closed it at a different price, "
                "record that instead:  python -m trading_bot journal --close "
                f"\"{case.id}\" --exit <price>",
            ),
            f"{word} after {trade.bars_held} bar(s)",
        )

    if trade is not None and trade.outcome is Outcome.EXPIRED and available > horizon:
        return LiveStatus(
            case, STATE_RESOLVED, available, current, unrealised, to_target, to_stop,
            trade.mfe_r, trade.mae_r, window_open,
            (
                f"The {horizon}-bar horizon has passed without either level being touched. "
                f"By the rules the base rate was measured under, this closes at the "
                f"horizon bar's close for {trade.r_multiple:+.2f}R.",
                "Settle it onto the record with:  python -m trading_bot ledger --resolve",
                "If you are still in it, you are now trading a plan nobody measured. "
                "Closing at market is the exit the record assumes.",
            ),
            "horizon elapsed without a barrier touch",
        )

    # Still running: neither level touched, inside the horizon.
    d = instrument.digits
    side_word = "above" if current > signal.entry else "below"
    moved = pips_between(instrument, current, signal.entry)
    favourable = (current - signal.entry) * sign > 0
    progress = (
        f"Price is {current:.{d}f}, {moved:.1f} pips {side_word} the entry: "
        f"{'on the right side' if favourable else 'against the trade'} so far, "
        f"{unrealised:+.2f}R from the fill." if unrealised is not None else
        f"Price is {current:.{d}f}."
    )
    distances = (
        f"{to_target:.1f} pips to the target ({signal.take_profit:.{d}f}), "
        f"{to_stop:.1f} pips to the stop ({signal.stop_loss:.{d}f}). "
        f"Best so far {trade.mfe_r:+.2f}R, worst {trade.mae_r:+.2f}R."
        if trade is not None else ""
    )
    elapsed = f"{available} of {horizon} bars elapsed; resolves by " \
              f"{config.clock.stamp(prediction.resolve_by)} ({resolve_left})."

    if window_open:
        advice = (
            progress,
            distances,
            elapsed,
            f"The entry window is still open for {deadline_left}. If you are not in and "
            f"still want it, the ticket stands: {ticket}."
            + (
                " Price has already moved in the trade's favour, so a limit at the entry "
                "may not fill; that is the plan working, not a reason to chase."
                if favourable and moved > 0.5 else ""
            ),
            "If you are in: do nothing. The stop and the target stay where they are.",
        )
        state = STATE_ENTRY_OPEN
    else:
        advice = (
            progress,
            distances,
            elapsed,
            f"If you are NOT in this trade: leave it. The entry window closed "
            f"{config.clock.stamp(prediction.entry_deadline)}, and a late entry is a "
            f"different trade with a worse ratio than the one measured.",
            "If you ARE in: hold and do nothing. Do not move the stop, do not take "
            "profit early. Every number on the card came from a plan that did neither.",
        )
        state = STATE_RUNNING
    return LiveStatus(
        case, state, available, current, unrealised, to_target, to_stop,
        trade.mfe_r if trade else None, trade.mae_r if trade else None, window_open,
        advice, f"running: {available} of {horizon} bars elapsed",
    )


# ---------------------------------------------------------------- rendering


def _fmt(price: float | None, digits: int) -> str:
    return "-" if price is None else f"{price:.{digits}f}"


def _dated(clock: Clock, moment: datetime) -> str:
    """'30 Apr 16:00' in local time — for tables whose rows span many days."""
    return clock.local(moment).strftime("%d %b %H:%M")


def describe_result(case: CaseFile, instrument: Instrument, clock: Clock) -> str:
    """What happened, in one paragraph a person would say out loud."""
    r = case.result
    if r is None:
        return "Still open."
    d = instrument.digits
    s = case.signal
    cash = f"{case.cash:+,.2f} {s.account_currency}" if case.cash is not None else ""
    fill = f"Filled at {_fmt(r.fill_price, d)}" if r.fill_price is not None else "Entered"
    when = f" on {clock.stamp(r.fill_time)}" if r.fill_time else ""
    bars = f"{r.bars_held} bar(s)" if r.bars_held is not None else "some bars"
    risk_pips = s.risk_pips or 1.0
    reward_pips = s.reward_pips or 1.0

    if r.outcome is Outcome.WIN or (r.outcome is Outcome.BREAKEVEN and r.is_win):
        worst = ""
        if r.mae_r is not None:
            worst = (
                f" On the way it went {abs(r.mae_r) * risk_pips:.1f} pips against you at worst "
                f"({r.mae_r:+.2f}R), which is {abs(r.mae_r):.0%} of the way to the stop."
            )
        return (
            f"RIGHT. {fill}{when}; price reached the target at {_fmt(s.take_profit, d)} "
            f"after {bars}{', ' + clock.stamp(r.exit_time) if r.exit_time else ''}.{worst} "
            f"Result {r.r_multiple:+.2f}R = {cash}."
        )
    if r.outcome is Outcome.LOSS or (r.outcome is Outcome.BREAKEVEN and r.is_loss):
        gapped = r.bars_held == 0 and r.r_multiple < -1.0 - 1e-9
        best = ""
        if r.mfe_r is not None and r.mfe_r > 0:
            reached = r.mfe_r * risk_pips / reward_pips
            best = (
                f" Before that it went {r.mfe_r * risk_pips:.1f} pips the right way "
                f"({r.mfe_r:+.2f}R), {reached:.0%} of the distance to the target."
            )
        elif r.mfe_r is not None:
            best = " It never traded in the trade's favour at all."
        how = (
            "the next bar opened through the stop, so the loss is worse than one R"
            if gapped else f"the stop at {_fmt(s.stop_loss, d)} was hit"
        )
        return (
            f"WRONG. {fill}{when}; {how} after {bars}"
            f"{', ' + clock.stamp(r.exit_time) if r.exit_time else ''}.{best} "
            f"Result {r.r_multiple:+.2f}R = {cash}."
        )
    if r.outcome is Outcome.EXPIRED:
        extremes = ""
        if r.mfe_r is not None and r.mae_r is not None:
            extremes = f" Best point {r.mfe_r:+.2f}R, worst {r.mae_r:+.2f}R."
        return (
            f"EXPIRED. {fill}{when}; neither the target nor the stop was touched inside the "
            f"horizon, so it closed at {_fmt(r.exit_price, d)} after {bars}.{extremes} "
            f"Result {r.r_multiple:+.2f}R = {cash}. A time-out is a real outcome and it counts."
        )
    return f"Closed at {_fmt(r.exit_price, d)} for {r.r_multiple:+.2f}R = {cash}."


def sparkline(path: tuple[PathBar, ...], width: int = 60) -> str:
    """The closes from fill to exit, as one line of block characters."""
    if len(path) < 2:
        return ""
    closes = [b.close for b in path]
    if len(closes) > width:
        step = len(closes) / width
        closes = [closes[int(i * step)] for i in range(width)]
    lo, hi = min(closes), max(closes)
    blocks = "▁▂▃▄▅▆▇█"
    if hi - lo < 1e-12:
        return blocks[3] * len(closes)
    return "".join(blocks[min(7, int((c - lo) / (hi - lo) * 7.999))] for c in closes)


def _path_rows(case: CaseFile, instrument: Instrument, clock: Clock) -> list[str]:
    """A handful of bars that tell the story: the fill, the extremes, the exit."""
    r = case.result
    if r is None or not r.path:
        return []
    s = case.signal
    d = instrument.digits
    sign = s.direction.sign
    fill = r.fill_price if r.fill_price is not None else s.entry
    risk = (fill - s.stop_loss) * sign
    if risk <= 0:
        return []
    bars = list(r.path)
    best = max(range(len(bars)), key=lambda i: (bars[i].high if sign > 0 else -bars[i].low))
    worst = min(range(len(bars)), key=lambda i: (bars[i].low if sign > 0 else -bars[i].high))
    picks = sorted({0, 1, best, worst, len(bars) - 2, len(bars) - 1} & set(range(len(bars))))
    lines = [f"    {'bar':>4}  {'closed (' + clock.abbrev() + ')':<13} {'high':>{d + 3}} "
             f"{'low':>{d + 3}} {'close':>{d + 3}} {'R at close':>10}"]
    previous = -1
    for i in picks:
        if i - previous > 1:
            lines.append("       ...")
        bar = bars[i]
        r_close = (bar.close - fill) * sign / risk
        tag = "  fill" if i == 0 else "  best" if i == best else "  worst" if i == worst else ""
        if i == len(bars) - 1:
            tag = "  exit"
        lines.append(
            f"    {i:>4}  {_dated(clock, bar.timestamp):<13} {bar.high:>{d + 3}.{d}f} "
            f"{bar.low:>{d + 3}.{d}f} {bar.close:>{d + 3}.{d}f} {r_close:>+10.2f}{tag}"
        )
        previous = i
    return lines


def format_case_line(case: CaseFile, clock: Clock, number: int | None = None) -> str:
    """One line per prediction, for the table at the top of the ledger."""
    s = case.signal
    side = "BUY " if s.direction is Direction.LONG else "SELL"
    tag = f"#{number:<4}" if number is not None else "     "
    if case.result is None:
        tail = f"{'OPEN':<8} {'':>7} {'':>5}"
    else:
        r = case.result
        bars = f"{r.bars_held}" if r.bars_held is not None else "-"
        tail = f"{r.verdict:<8} {r.r_multiple:>+7.2f} {bars:>5}"
    cash = f"{case.cash:>+10,.2f}" if case.cash is not None else f"{'':>10}"
    return (
        f"  {tag} {_dated(clock, case.made_at):<13} {s.symbol:<7} {side} {s.grade:<2} "
        f"{s.confidence:>4.0%} {s.risk_reward:>5.1f}R  {tail} {cash}"
    )


def format_case(
    case: CaseFile, clock: Clock, config: Config, number: int | None = None, width: int = 78
) -> list[str]:
    """The full case file: the call, what the model saw, and what happened."""
    s = case.signal
    instrument = get_instrument(s.symbol)
    d = instrument.digits
    side = "BUY " if s.direction is Direction.LONG else "SELL"
    tag = f"#{number}  " if number is not None else ""
    label = (
        "REPLAY: the call the model would have made at"
        if case.origin == ORIGIN_REPLAY else "PREDICTION made"
    )
    result = case.result
    outcome = (
        "OPEN"
        if result is None
        else f"{result.verdict}  {result.r_multiple:+.2f}R"
        + (f"  ({case.cash:+,.2f} {s.account_currency})" if case.cash is not None else "")
    )
    bar = "=" * width
    dash = "-" * width
    lines = [
        bar,
        f"  {tag}{side} {s.symbol}  [{s.grade}] confidence {s.confidence:.0%}  "
        f"{s.risk_reward:.1f}R     {outcome}",
        f"  {instrument.describe()}",
        f"  {label} {clock.stamp(s.issued_at)}",
        bar,
    ]
    if case.prediction is not None:
        p = case.prediction
        lines += wrap(p.claim, indent=" " * 17, first="  THE CALL       ")
        lines.append(
            f"  Entry {_fmt(s.entry, d)}   stop {_fmt(s.stop_loss, d)} ({s.risk_pips:.1f} pips)"
            f"   target {_fmt(s.take_profit, d)} ({s.reward_pips:.1f} pips)"
        )
        lines.append(
            f"  Size {s.position_lots:.2f} lots, risking {s.risk_amount:,.2f} "
            f"{s.account_currency} to make {s.risk_amount * s.risk_reward:,.2f}"
        )
        lines.append(
            f"  Enter by {clock.stamp(p.entry_deadline)}; resolves by "
            f"{clock.stamp(p.resolve_by)} ({p.horizon_bars} bars)"
        )
        if case.origin == ORIGIN_FORWARD:
            lines += wrap(p.base_rate.headline, indent=" " * 17, first="  Base rate      ")
    lines.append("")

    # What the model saw.
    read = case.readings
    session = case.session or session_label(s.issued_at)
    lines.append(
        f"  WHAT THE MODEL SAW  ({s.score:.0f} of {s.max_score:.0f} points, "
        f"{s.timeframe.name} close {_fmt(read.get('price', s.entry), d)}, {session})"
    )
    if not case.snapshot_complete:
        lines.append("    (recorded before the ledger kept snapshots: the checks that did not")
        lines.append("     fire are inferred from the strategy's table, not from the record)")
    for row in case.checks:
        mark = "+" if row.fired else "-"
        text = row.detail if row.fired else f"not met: {row.title}"
        lines += wrap(
            f"{row.code:<10} {row.weight:>3.0f}  {text}", indent=" " * 20, first=f"    {mark} "
        )
    if read:
        parts = []
        for key, label, digits in (
            ("atr", "ATR", d), ("rsi", "RSI", 1), ("adx", "ADX", 1),
            ("plus_di", "+DI", 1), ("minus_di", "-DI", 1),
        ):
            value = read.get(key)
            if value is not None:
                parts.append(f"{label} {value:.{digits}f}")
        emas = [read.get("ema_fast"), read.get("ema_slow"), read.get("ema_trend")]
        if None not in emas:
            order = ">" if emas[0] > emas[1] > emas[2] else "<" if emas[0] < emas[1] < emas[2] else "~"
            parts.append(
                f"EMA {config.strategy.ema_fast}/{config.strategy.ema_slow}/"
                f"{config.strategy.ema_trend} {emas[0]:.{d}f} {order} {emas[1]:.{d}f} "
                f"{order} {emas[2]:.{d}f}"
            )
        if read.get("htf_trend"):
            parts.append(f"{config.data.htf_timeframe} trend {read['htf_trend']}")
        if read.get("trend"):
            parts.append(f"{s.timeframe.name} structure {read['trend']}")
        if parts:
            lines += wrap("; ".join(parts), indent="      ", first="    Readings: ")
    if s.warnings:
        for warning in s.warnings:
            lines += wrap(warning, indent="      ", first="    ! ")
    lines.append("")

    # What happened.
    if result is None:
        lines.append("  WHAT HAPPENED")
        lines.append("    Nothing yet. The market has not answered; see the open predictions")
        lines.append("    above for where it stands and what to do.")
    else:
        lines.append("  WHAT HAPPENED")
        lines += wrap(describe_result(case, instrument, clock), indent="    ", first="    ")
        if result.r_basis == "planned entry":
            lines.append("    (closed by hand; R measured against the planned entry, not a fill)")
        rows = _path_rows(case, instrument, clock)
        if rows:
            lines.append("")
            lines += rows
        spark = sparkline(result.path)
        if spark:
            lines.append("")
            lines.append(f"    closes, fill to exit:  {spark}")
    lines.append(dash)
    return lines


def format_live(status: LiveStatus, clock: Clock, config: Config) -> list[str]:
    """One open prediction: where it stands, and what to do now."""
    case = status.case
    s = case.signal
    instrument = get_instrument(s.symbol)
    d = instrument.digits
    side = "BUY " if s.direction is Direction.LONG else "SELL"
    lines = [
        f"  {side} {s.symbol}  [{s.grade}] {s.confidence:.0%}  {s.risk_reward:.1f}R   "
        f"{status.state}",
        f"    made {clock.stamp(s.issued_at)}   entry {_fmt(s.entry, d)}  "
        f"stop {_fmt(s.stop_loss, d)}  target {_fmt(s.take_profit, d)}",
    ]
    for sentence in status.advice:
        if sentence:
            lines += wrap(sentence, indent="      ", first="    - ")
    return lines


def _table(headers: list[str], rows: list[list[str]], aligns: str) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.ljust(widths[i]) if aligns[i] == "l" else cell.rjust(widths[i]))
        return "    " + "  ".join(out)
    return [line(headers), "    " + "-" * (sum(widths) + 2 * (len(widths) - 1))] + [
        line(row) for row in rows
    ]


def _bucket_rows(buckets: list[Bucket]) -> list[list[str]]:
    return [
        [
            b.label, str(b.trades), str(b.wins), str(b.losses),
            f"{b.win_rate:.0%}", f"{b.interval.low:.0%}-{b.interval.high:.0%}",
            f"{b.expectancy_r:+.2f}R", f"{b.total_r:+.1f}R", f"{b.cash:+,.0f}",
        ]
        for b in buckets
    ]


BUCKET_HEADERS = ["", "n", "right", "wrong", "win", "95% interval", "exp", "total", "cash"]


def format_scorecards(cases: list[CaseFile], config: Config) -> list[str]:
    """Every breakdown that is worth a table, each with n beside every rate."""
    confidence = config.target.confidence
    resolved = [c for c in cases if c.result is not None]
    if not resolved:
        return ["  No resolved predictions yet, so there is nothing to break down."]
    lines: list[str] = []

    sections = [
        ("BY CONFIDENCE THE MODEL QUOTED  (does the number mean anything?)",
         calibration(resolved, confidence)),
        ("BY GRADE", breakdown(resolved, by_grade, confidence, order=GRADE_ORDER)),
        ("BY PAIR", breakdown(resolved, by_symbol, confidence)),
        ("BY DIRECTION", breakdown(resolved, by_direction, confidence, order=["buy", "sell"])),
        ("BY SESSION THE CALL WAS MADE IN", breakdown(resolved, by_session, confidence)),
        ("BY MONTH", breakdown(
            resolved, by_month, confidence, order=sorted({by_month(c) for c in resolved})
        )),
    ]
    strategies = breakdown(resolved, by_strategy, confidence)
    if len(strategies) > 1:
        sections.append(("BY MODEL", strategies))

    for title, buckets in sections:
        if not buckets:
            continue
        lines.append(f"  {title}")
        lines += _table(BUCKET_HEADERS, _bucket_rows(buckets), "lrrrrrrrr")
        lines.append("")

    effects = check_attribution(resolved, confidence)
    if effects:
        lines.append("  BY CHECK  (win rate when the check fired vs when it did not)")
        rows = []
        for e in effects:
            fired = f"{e.fired.win_rate:.0%} (n={e.fired.trades})" if e.fired.trades else "-"
            missing = (
                f"{e.missing.win_rate:.0%} (n={e.missing.trades})" if e.missing.trades else "-"
            )
            diff = f"{e.difference * 100:+.0f} pts" if e.difference is not None else "n/a"
            rows.append([e.code, f"{e.weight:.0f}", fired, missing, diff])
        lines += _table(["check", "weight", "fired", "missing", "diff"], rows, "lrrrr")
        lines.append("    Every signal cleared the threshold, so most checks fired on most calls")
        lines.append("    and the 'missing' column is thin by construction. Read n before diff.")
        lines.append("")
    if len(resolved) < config.target.min_sample:
        lines += wrap(
            f"{len(resolved)} resolved prediction(s) is below the {config.target.min_sample} "
            f"this ledger needs before any row above means anything on its own. The "
            f"intervals say how wide the honest answer still is.",
            indent="  ",
        )
    return lines


def format_table(cases: list[CaseFile], clock: Clock, start_number: int = 1) -> list[str]:
    """Every prediction as one line, newest first."""
    header = (
        f"  {'#':<5} {'made (' + clock.abbrev() + ')':<13} {'pair':<7} {'side'} {'gr':<2} "
        f"{'conf':>4} {'R:R':>6}  {'verdict':<8} {'R':>7} {'bars':>5} {'cash':>10}"
    )
    lines = [header, "  " + "-" * 87]
    total = len(cases)
    for offset, case in enumerate(reversed(cases)):
        lines.append(format_case_line(case, clock, start_number + total - 1 - offset))
    return lines
