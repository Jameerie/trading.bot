"""Configuration loading and validation.

Config is TOML, read with the stdlib ``tomllib``. Validation is strict and
happens at load time: a bad risk-to-reward floor or a negative risk percentage
should fail before any market data is fetched, not halfway through a scan.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .clock import DEFAULT_ZONE, Clock
from .errors import ConfigError
from .instruments import expand_symbols, group_names, normalise_symbol

# The product rule from the README. Config may raise this floor but never lower it.
ABSOLUTE_MIN_RR = 4.0


@dataclass(frozen=True)
class AccountConfig:
    """Account size and per-trade risk."""

    balance: float = 10_000.0
    currency: str = "USD"
    risk_per_trade_pct: float = 1.0
    max_concurrent_risk_pct: float = 3.0

    def validate(self) -> None:
        if self.balance <= 0:
            raise ConfigError(f"account.balance must be positive, got {self.balance}")
        if not 0 < self.risk_per_trade_pct <= 10:
            raise ConfigError(
                f"account.risk_per_trade_pct must be in (0, 10], got {self.risk_per_trade_pct}"
            )
        if self.max_concurrent_risk_pct < self.risk_per_trade_pct:
            raise ConfigError(
                "account.max_concurrent_risk_pct cannot be below risk_per_trade_pct"
            )


@dataclass(frozen=True)
class RiskConfig:
    """The rules that decide whether a setup is allowed to become a signal."""

    min_risk_reward: float = 4.0
    max_risk_reward: float = 20.0
    stop_atr_multiple: float = 1.5
    min_stop_pips: float = 8.0
    max_stop_pips: float = 60.0
    structure_buffer_atr: float = 0.25

    def validate(self) -> None:
        if self.min_risk_reward < ABSOLUTE_MIN_RR:
            raise ConfigError(
                f"risk.min_risk_reward may not go below the project floor of "
                f"{ABSOLUTE_MIN_RR}; got {self.min_risk_reward}. This rule is the product."
            )
        if self.max_risk_reward <= self.min_risk_reward:
            raise ConfigError("risk.max_risk_reward must exceed min_risk_reward")
        if self.stop_atr_multiple <= 0:
            raise ConfigError("risk.stop_atr_multiple must be positive")
        if self.min_stop_pips <= 0 or self.max_stop_pips <= self.min_stop_pips:
            raise ConfigError("risk stop pip bounds are inconsistent")


@dataclass(frozen=True)
class StrategyConfig:
    """Strategy selection and its quality dial."""

    name: str = "trend_pullback"
    min_confluence: float = 0.70
    ema_fast: int = 21
    ema_slow: int = 50
    ema_trend: int = 200
    atr_period: int = 14
    rsi_period: int = 14
    adx_period: int = 14
    adx_min: float = 20.0
    swing_left: int = 2
    swing_right: int = 2
    max_pullback_bars: int = 12
    sessions: list[str] = field(default_factory=lambda: ["london", "newyork"])
    avoid_weekend: bool = True

    def validate(self) -> None:
        if not 0 < self.min_confluence <= 1:
            raise ConfigError(
                f"strategy.min_confluence is a 0-1 fraction, got {self.min_confluence}"
            )
        if not self.ema_fast < self.ema_slow < self.ema_trend:
            raise ConfigError("strategy EMA periods must satisfy fast < slow < trend")
        for name in ("atr_period", "rsi_period", "adx_period", "swing_left", "swing_right"):
            if getattr(self, name) < 1:
                raise ConfigError(f"strategy.{name} must be >= 1")


@dataclass(frozen=True)
class BacktestConfig:
    """Simulation realism knobs."""

    spread_pips: float | None = None  # None = use the instrument's typical spread
    commission_per_lot: float = 7.0
    slippage_pips: float = 0.2
    max_bars_in_trade: int = 200
    entry_expiry_bars: int = 3

    def validate(self) -> None:
        if self.spread_pips is not None and self.spread_pips < 0:
            raise ConfigError("backtest.spread_pips cannot be negative")
        if self.slippage_pips < 0:
            raise ConfigError("backtest.slippage_pips cannot be negative")
        if self.max_bars_in_trade < 1:
            raise ConfigError("backtest.max_bars_in_trade must be >= 1")


@dataclass(frozen=True)
class DataConfig:
    """Where candles come from."""

    source: str = "csv"
    symbols: list[str] = field(default_factory=lambda: ["EURUSD"])
    timeframe: str = "H1"
    htf_timeframe: str = "H4"
    csv_dir: str = "data/samples"
    provider: str = "twelvedata"
    api_key_env: str = "TRADING_BOT_API_KEY"
    lookback_bars: int = 500

    def validate(self) -> None:
        if self.source not in ("csv", "rest", "synthetic"):
            raise ConfigError(
                f"data.source must be one of csv, rest, synthetic; got {self.source!r}"
            )
        if not self.symbols:
            raise ConfigError("data.symbols must list at least one symbol")
        if self.lookback_bars < 60:
            raise ConfigError("data.lookback_bars below 60 leaves indicators unwarmed")
        for entry in self.symbols:
            text = str(entry).strip()
            if text.lower() in group_names():
                continue
            if len(normalise_symbol(text)) < 6:
                raise ConfigError(
                    f"data.symbols entry {entry!r} is neither a 6-letter pair nor one of "
                    f"the groups {', '.join(group_names())}"
                )

    @property
    def resolved_symbols(self) -> list[str]:
        """Concrete symbols to scan, with group keywords expanded.

        ``symbols = ["all"]`` is the whole registry; ``["majors", "XAUUSD"]``
        works too. Everything downstream reads this rather than ``symbols``, so
        a group name is usable anywhere a pair is.
        """
        return expand_symbols(self.symbols)

    @property
    def api_key(self) -> str | None:
        """Read the provider key from the environment. Never stored in the file."""
        return os.environ.get(self.api_key_env)


@dataclass(frozen=True)
class DisplayConfig:
    """How output is presented to the human reading it.

    ``timezone`` is a display concern only. Nothing in the decision path reads
    it — sessions, candles and the journal stay in UTC — but every time shown to
    the user is converted through it, because a signal timestamped 13:00 UTC is
    useless to someone whose day is measured in WAT.
    """

    timezone: str = DEFAULT_ZONE
    detail: str = "full"

    def validate(self) -> None:
        if self.detail not in ("full", "brief"):
            raise ConfigError(
                f"display.detail must be 'full' or 'brief'; got {self.detail!r}"
            )
        if not self.timezone.strip():
            raise ConfigError("display.timezone must not be empty")

    @property
    def clock(self) -> Clock:
        """The clock every renderer should use."""
        return Clock(self.timezone)


@dataclass(frozen=True)
class TargetConfig:
    """The performance bar the system holds itself to."""

    win_rate: float = 0.85
    min_sample: int = 30
    confidence: float = 0.95

    def validate(self) -> None:
        if not 0 < self.win_rate < 1:
            raise ConfigError(f"target.win_rate is a 0-1 fraction, got {self.win_rate}")
        if self.min_sample < 5:
            raise ConfigError("target.min_sample below 5 is not a meaningful sample")
        if not 0.5 <= self.confidence < 1:
            raise ConfigError("target.confidence must be in [0.5, 1)")


@dataclass(frozen=True)
class LimitsConfig:
    """Account-level loss limits that pause advice after a bad run.

    These are the rules a proprietary trading firm enforces on a funded account,
    and they are worth keeping on your own money for the same reason: the danger
    after a run of losses is rarely that the next setup is poor. It is that a
    human who has just lost money takes setups they would otherwise skip.

    Nothing here places, cancels, or resizes anything. A breach puts a warning on
    the signal card and the human still decides — same as every other output of
    this program.
    """

    enabled: bool = True
    daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 10.0

    def validate(self) -> None:
        if not 0 < self.daily_loss_pct <= 100:
            raise ConfigError(
                f"limits.daily_loss_pct must be in (0, 100], got {self.daily_loss_pct}"
            )
        if not 0 < self.max_drawdown_pct <= 100:
            raise ConfigError(
                f"limits.max_drawdown_pct must be in (0, 100], got {self.max_drawdown_pct}"
            )
        if self.max_drawdown_pct < self.daily_loss_pct:
            raise ConfigError(
                "limits.max_drawdown_pct cannot be below daily_loss_pct: a single day "
                "would then be allowed to breach the account-wide limit"
            )


@dataclass(frozen=True)
class Config:
    """The whole configuration tree."""

    account: AccountConfig = field(default_factory=AccountConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    data: DataConfig = field(default_factory=DataConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    journal_path: str = "reports/journal.jsonl"

    def validate(self) -> "Config":
        self.account.validate()
        self.risk.validate()
        self.strategy.validate()
        self.backtest.validate()
        self.data.validate()
        self.target.validate()
        self.limits.validate()
        self.display.validate()
        return self

    @property
    def clock(self) -> Clock:
        """Shortcut to the display clock — every renderer needs it."""
        return self.display.clock


def _build(section: dict, cls, name: str):
    """Instantiate a config dataclass, rejecting unknown keys.

    Silently ignoring a typo'd key is how a user ends up trading with settings
    they think they changed, so unknown keys are a hard error.
    """
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(section) - known
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [{name}]: {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )
    return cls(**section)


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate config from TOML, falling back to defaults.

    Defaults are chosen so that ``load_config(None)`` produces a usable,
    rule-compliant system with no file present.
    """
    if path is None:
        return Config().validate()
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} is not valid TOML: {exc}") from exc

    top_unknown = set(raw) - {
        "account", "risk", "strategy", "backtest", "data", "target", "limits",
        "display", "journal_path",
    }
    if top_unknown:
        raise ConfigError(f"unknown top-level section(s): {', '.join(sorted(top_unknown))}")

    return Config(
        account=_build(raw.get("account", {}), AccountConfig, "account"),
        risk=_build(raw.get("risk", {}), RiskConfig, "risk"),
        strategy=_build(raw.get("strategy", {}), StrategyConfig, "strategy"),
        backtest=_build(raw.get("backtest", {}), BacktestConfig, "backtest"),
        data=_build(raw.get("data", {}), DataConfig, "data"),
        target=_build(raw.get("target", {}), TargetConfig, "target"),
        limits=_build(raw.get("limits", {}), LimitsConfig, "limits"),
        display=_build(raw.get("display", {}), DisplayConfig, "display"),
        journal_path=raw.get("journal_path", "reports/journal.jsonl"),
    ).validate()
