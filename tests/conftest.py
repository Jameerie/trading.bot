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


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """Run every test as if the developer's shell held no credentials.

    ``server.py`` deliberately falls back to ``TRADING_BOT_TOKEN`` when no token
    is passed in, which is the right behaviour and is tested explicitly. The
    catch is that anyone who has followed SETUP.md has that variable exported,
    and five web tests then went red on their machine and nowhere else — the
    worst kind of failure, because it looks like the code broke.

    Tests that need either variable set still set it themselves with
    ``monkeypatch.setenv``, which applies after this fixture.
    """
    for name in ("TRADING_BOT_TOKEN", "TRADING_BOT_API_KEY"):
        monkeypatch.delenv(name, raising=False)


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
