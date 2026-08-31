"""Strategies and the confluence machinery they score with."""

from ..errors import StrategyError
from .base import MarketContext, Strategy, build_context
from .confluence import Check, ConfluenceEngine, ConfluenceResult
from .trend_pullback import DEFAULT_CHECKS, TrendPullbackStrategy

REGISTRY = {
    TrendPullbackStrategy.name: TrendPullbackStrategy,
}


def get_strategy(name: str) -> Strategy:
    """Instantiate a strategy by config name."""
    key = name.strip().lower()
    if key not in REGISTRY:
        raise StrategyError(
            f"unknown strategy {name!r}. Available: {', '.join(sorted(REGISTRY))}"
        )
    return REGISTRY[key]()


__all__ = [
    "MarketContext",
    "Strategy",
    "build_context",
    "Check",
    "ConfluenceEngine",
    "ConfluenceResult",
    "TrendPullbackStrategy",
    "DEFAULT_CHECKS",
    "REGISTRY",
    "get_strategy",
]
