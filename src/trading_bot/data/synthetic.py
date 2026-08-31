"""Deterministic synthetic market data.

Used by the test suite and the demo so that neither needs a network connection
or a vendor key. The generator is seeded, so the same seed always yields the
same series — a backtest whose fixtures shift between runs cannot be trusted.

This is a random walk with regime switching. It is *not* a model of real FX and
must never be used to make claims about live performance.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from ..models import Candle, Timeframe
from .base import validate_series


def generate(
    bars: int = 600,
    start_price: float = 1.1000,
    timeframe: Timeframe = Timeframe.H1,
    seed: int = 42,
    volatility: float = 0.0008,
    trend_strength: float = 0.35,
    regime_bars: int = 60,
    start: datetime | None = None,
) -> list[Candle]:
    """Generate a deterministic OHLCV series with alternating trend regimes.

    ``trend_strength`` is the drift as a fraction of per-bar volatility; regimes
    flip roughly every ``regime_bars`` so the series contains both trending and
    ranging stretches, which is what a selective strategy has to survive.
    """
    if bars < 1:
        raise ValueError("bars must be >= 1")
    rng = random.Random(seed)
    begin = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    step = timedelta(minutes=timeframe.minutes)

    candles: list[Candle] = []
    price = start_price
    direction = 1
    bars_in_regime = 0

    for i in range(bars):
        bars_in_regime += 1
        # Flip regime on a fuzzy schedule so the period is not learnable.
        if bars_in_regime >= regime_bars and rng.random() < 0.25:
            direction = rng.choice([1, -1, 0])
            bars_in_regime = 0

        drift = direction * trend_strength * volatility
        shock = rng.gauss(0.0, volatility)
        open_price = price
        close_price = max(open_price + drift + shock, 0.0001)

        # Wick size scales with the bar's own body so ranges look plausible.
        body = abs(close_price - open_price)
        wick = volatility * (0.3 + rng.random() * 0.9) + body * 0.25
        high = max(open_price, close_price) + wick * rng.random()
        low = min(open_price, close_price) - wick * rng.random()

        candles.append(
            Candle(
                timestamp=begin + step * i,
                open=round(open_price, 5),
                high=round(max(high, open_price, close_price), 5),
                low=round(min(low, open_price, close_price), 5),
                close=round(close_price, 5),
                volume=round(500 + abs(shock) / volatility * 400 + rng.random() * 200, 2),
            )
        )
        price = close_price

    return validate_series(candles, "synthetic")


def generate_trending(
    bars: int = 300, seed: int = 7, up: bool = True, **kwargs
) -> list[Candle]:
    """A directional series that still pulls back — used to assert setups are found.

    ``trend_strength`` is kept below 1.0 on purpose. Drift much larger than the
    per-bar volatility produces a monotonic line with no swing points at all, and
    a pullback strategy correctly finds nothing to do in it. Such a series tests
    nothing except the generator.
    """
    return generate(
        bars=bars,
        seed=seed,
        trend_strength=0.5 if up else -0.5,
        regime_bars=10_000,  # the regime never flips, so the bias is constant
        **kwargs,
    )


class SyntheticSource:
    """DataSource wrapper so synthetic data is usable anywhere a source is."""

    def __init__(self, seed: int = 42, **kwargs) -> None:
        self.seed = seed
        self.kwargs = kwargs

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        # Offset the seed by the symbol so different pairs are not identical.
        symbol_seed = self.seed + sum(ord(ch) for ch in symbol.upper())
        start_price = 150.0 if symbol.upper().endswith("JPY") else 1.1000
        vol = 0.08 if symbol.upper().endswith("JPY") else 0.0008
        return generate(
            bars=limit,
            seed=symbol_seed,
            timeframe=timeframe,
            start_price=start_price,
            volatility=vol,
            **self.kwargs,
        )
