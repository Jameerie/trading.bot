"""Weighted confluence scoring.

The premise: no single indicator earns a 1:4 trade. A setup qualifies when
several independent readings of the market agree — trend, structure, location,
momentum, and timing. Each check that fires contributes its weight, and the
score is the fraction of the available weight that fired.

The score is the quality dial. Raising ``strategy.min_confluence`` takes fewer,
better setups; lowering it takes more, worse ones. ``calibrate`` exists to find
where that trade-off actually sits on real data, rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..models import Direction, Reason
from .base import MarketContext

CheckFn = Callable[[MarketContext, Direction], tuple[bool, str]]


@dataclass(frozen=True)
class Check:
    """One named confluence condition."""

    code: str
    weight: float
    fn: CheckFn

    def run(self, context: MarketContext, direction: Direction) -> Reason | None:
        fired, detail = self.fn(context, direction)
        return Reason(self.code, detail, self.weight) if fired else None


@dataclass(frozen=True)
class ConfluenceResult:
    """The outcome of scoring one direction at one bar."""

    score: float
    max_score: float
    reasons: tuple[Reason, ...]
    missing: tuple[str, ...]

    @property
    def fraction(self) -> float:
        return self.score / self.max_score if self.max_score > 0 else 0.0


class ConfluenceEngine:
    """Runs a set of checks and totals their weights."""

    def __init__(self, checks: list[Check]) -> None:
        if not checks:
            raise ValueError("confluence engine needs at least one check")
        codes = [c.code for c in checks]
        if len(set(codes)) != len(codes):
            raise ValueError(f"duplicate check codes: {codes}")
        self.checks = checks

    @property
    def max_score(self) -> float:
        return sum(c.weight for c in self.checks)

    def score(self, context: MarketContext, direction: Direction) -> ConfluenceResult:
        reasons: list[Reason] = []
        missing: list[str] = []
        total = 0.0
        for check in self.checks:
            reason = check.run(context, direction)
            if reason is None:
                missing.append(check.code)
            else:
                reasons.append(reason)
                total += reason.weight
        return ConfluenceResult(
            score=round(total, 2),
            max_score=self.max_score,
            reasons=tuple(reasons),
            missing=tuple(missing),
        )
