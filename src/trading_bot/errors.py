"""Exception types for the trading advisor.

Every failure mode gets a named type so callers can tell a bad config apart from
bad market data apart from a setup that violates the risk rules.
"""

from __future__ import annotations


class TradingBotError(Exception):
    """Base class for every error this package raises."""


class ConfigError(TradingBotError):
    """Configuration is missing, malformed, or internally inconsistent."""


class DataError(TradingBotError):
    """Market data is unusable: wrong shape, out of order, or not UTC."""


class RiskError(TradingBotError):
    """A setup violates a risk rule that must never be bent."""


class StrategyError(TradingBotError):
    """A strategy was asked for something it cannot produce."""
