"""Core value types.

Everything here is frozen. A Candle that can be mutated after a strategy has
seen it is a look-ahead bug waiting to happen, and a Signal that can be edited
after issue makes the journal untrustworthy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .errors import DataError


class Direction(Enum):
    """Which way a setup points."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short. Lets price math stay direction-agnostic."""
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Outcome(Enum):
    """How a simulated or journalled trade finished."""

    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"
    OPEN = "open"
    EXPIRED = "expired"


class Timeframe(Enum):
    """Supported bar intervals, with their length in minutes."""

    M5 = 5
    M15 = 15
    M30 = 30
    H1 = 60
    H4 = 240
    D1 = 1440

    @classmethod
    def parse(cls, text: str) -> "Timeframe":
        key = text.strip().upper()
        try:
            return cls[key]
        except KeyError as exc:
            valid = ", ".join(t.name for t in cls)
            raise DataError(f"unknown timeframe {text!r}; expected one of {valid}") from exc

    @property
    def minutes(self) -> int:
        return self.value


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar, timestamped at its **open** in UTC.

    Validation happens here rather than in each data source so that no malformed
    bar can reach the strategy layer from any direction.
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise DataError(f"candle timestamp {self.timestamp!r} is naive; UTC required")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise DataError(f"candle {name} is not finite: {value!r}")
            if value <= 0:
                raise DataError(f"candle {name} must be positive, got {value!r}")
        if self.high < self.low:
            raise DataError(f"candle high {self.high} is below low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise DataError(f"candle open {self.open} outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise DataError(f"candle close {self.close} outside [{self.low}, {self.high}]")

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_ratio(self) -> float:
        """Body as a fraction of range. Near 1.0 is a decisive bar, near 0 a doji."""
        return self.body / self.range if self.range > 0 else 0.0

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass(frozen=True)
class Reason:
    """One line of the 'why' behind a signal.

    Signals must be explainable. Each confluence check that fires contributes a
    Reason, and the score is the sum of their weights, so the printed rationale
    and the number always agree.
    """

    code: str
    detail: str
    weight: float

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.detail} (+{self.weight:.0f})"


@dataclass(frozen=True)
class Signal:
    """A complete 'here is what to do' instruction for a human.

    A Signal is only ever constructed through ``signals.build_signal``, which
    enforces the risk-to-reward floor. Constructing one directly bypasses that
    check, so don't.
    """

    symbol: str
    timeframe: Timeframe
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    issued_at: datetime
    score: float
    max_score: float
    risk_reward: float
    risk_pips: float
    reward_pips: float
    position_units: float = 0.0
    position_lots: float = 0.0
    risk_amount: float = 0.0
    account_currency: str = "USD"
    reasons: tuple[Reason, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    strategy: str = "unknown"

    @property
    def confidence(self) -> float:
        """Score as a 0-1 fraction of the maximum available confluence."""
        return self.score / self.max_score if self.max_score > 0 else 0.0

    @property
    def grade(self) -> str:
        """Letter grade, so a glance is enough to triage a list of setups."""
        c = self.confidence
        if c >= 0.85:
            return "A+"
        if c >= 0.75:
            return "A"
        if c >= 0.65:
            return "B"
        if c >= 0.55:
            return "C"
        return "D"

    def to_dict(self) -> dict:
        """Serialise for the journal. Enums and datetimes become plain strings."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.name,
            "direction": self.direction.value,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "issued_at": self.issued_at.isoformat(),
            "score": self.score,
            "max_score": self.max_score,
            "confidence": round(self.confidence, 4),
            "grade": self.grade,
            "risk_reward": self.risk_reward,
            "risk_pips": self.risk_pips,
            "reward_pips": self.reward_pips,
            "position_units": self.position_units,
            "position_lots": self.position_lots,
            "risk_amount": self.risk_amount,
            "account_currency": self.account_currency,
            "strategy": self.strategy,
            "reasons": [
                {"code": r.code, "detail": r.detail, "weight": r.weight} for r in self.reasons
            ],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Trade:
    """A signal carried through to a resolved outcome by the backtester."""

    signal: Signal
    entry_time: datetime
    exit_time: datetime | None
    exit_price: float | None
    outcome: Outcome
    r_multiple: float
    bars_held: int
    mae_r: float = 0.0
    mfe_r: float = 0.0

    @property
    def is_win(self) -> bool:
        return self.outcome is Outcome.WIN

    @property
    def is_loss(self) -> bool:
        return self.outcome is Outcome.LOSS

    @property
    def is_resolved(self) -> bool:
        return self.outcome in (Outcome.WIN, Outcome.LOSS, Outcome.BREAKEVEN)

    def to_dict(self) -> dict:
        return {
            "symbol": self.signal.symbol,
            "direction": self.signal.direction.value,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "entry": self.signal.entry,
            "exit_price": self.exit_price,
            "stop_loss": self.signal.stop_loss,
            "take_profit": self.signal.take_profit,
            "outcome": self.outcome.value,
            "r_multiple": round(self.r_multiple, 4),
            "bars_held": self.bars_held,
            "grade": self.signal.grade,
            "score": self.signal.score,
        }


def utc_now() -> datetime:
    """Single source of 'now'. Tests monkeypatch this rather than datetime."""
    return datetime.now(timezone.utc)
