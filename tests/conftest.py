"""Shared fixtures.

Everything here is deterministic and offline. A test that needs the network is a
test that fails for reasons unrelated to the code.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_bot.config import Config, load_config  # noqa: E402
from trading_bot.data.synthetic import generate, generate_trending  # noqa: E402
from trading_bot.instruments import get_instrument  # noqa: E402
from trading_bot.models import Candle  # noqa: E402

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_candle(offset_hours: int, o: float, h: float, l: float, c: float, v: float = 100.0):
    """Build one candle at a fixed offset from the fixture epoch."""
    return Candle(START + timedelta(hours=offset_hours), o, h, l, c, v)


def make_series(prices, wick: float = 0.0005):
    """Build a candle series from closes, with symmetric wicks."""
    out = []
    for i, price in enumerate(prices):
        prev = prices[i - 1] if i else price
        out.append(
            Candle(
                timestamp=START + timedelta(hours=i),
                open=prev,
                high=max(prev, price) + wick,
                low=min(prev, price) - wick,
                close=price,
            )
        )
    return out


@pytest.fixture
def config() -> Config:
    return load_config(None)


@pytest.fixture
def eurusd():
    return get_instrument("EURUSD")


@pytest.fixture
def usdjpy():
    return get_instrument("USDJPY")


@pytest.fixture
def random_series():
    return generate(bars=900, seed=42)


@pytest.fixture
def trending_series():
    return generate_trending(bars=700, seed=7)
