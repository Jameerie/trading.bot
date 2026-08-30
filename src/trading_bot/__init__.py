"""trading.bot - a forex trading advisor.

It reads the market, finds setups that pay at least 1:4, and tells a human what
to do. It does not place trades, and it is not built to.
"""

__version__ = "1.0.0"

from .config import Config, load_config
from .errors import ConfigError, DataError, RiskError, StrategyError, TradingBotError
from .models import Candle, Direction, Outcome, Signal, Timeframe, Trade

__all__ = [
    "__version__",
    "Config",
    "load_config",
    "Candle",
    "Direction",
    "Outcome",
    "Signal",
    "Timeframe",
    "Trade",
    "TradingBotError",
    "ConfigError",
    "DataError",
    "RiskError",
    "StrategyError",
]
