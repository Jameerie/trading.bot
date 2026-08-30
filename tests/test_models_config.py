"""Value types, configuration, instruments and sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.config import (
    AccountConfig,
    BacktestConfig,
    Config,
    DataConfig,
    StrategyConfig,
    TargetConfig,
    load_config,
)
from trading_bot.errors import ConfigError, DataError
from trading_bot.instruments import (
    get_instrument,
    pips_between,
    price_from_pips,
    round_price,
    same_price,
)
from trading_bot.models import Candle, Direction, Signal, Timeframe, utc_now
from trading_bot.sessions import (
    LONDON,
    active_sessions,
    get_session,
    in_any_session,
    is_weekend,
    session_label,
)

from conftest import START, make_candle


class TestCandle:
    def test_rejects_naive_timestamps(self):
        with pytest.raises(DataError, match="naive"):
            Candle(datetime(2024, 1, 1), 1.1, 1.2, 1.0, 1.15)

    @pytest.mark.parametrize(
        "o,h,l,c",
        [
            (1.5, 1.2, 1.0, 1.1),   # open above high
            (1.1, 1.2, 1.0, 1.5),   # close above high
            (1.1, 1.2, 1.15, 1.18), # low above open
            (1.1, 1.0, 1.2, 1.15),  # high below low
        ],
    )
    def test_rejects_impossible_bars(self, o, h, l, c):
        with pytest.raises(DataError):
            Candle(START, o, h, l, c)

    def test_rejects_non_positive_prices(self):
        with pytest.raises(DataError):
            Candle(START, 0.0, 1.2, 0.0, 1.1)

    def test_is_immutable(self):
        candle = make_candle(0, 1.1, 1.2, 1.0, 1.15)
        with pytest.raises(Exception):
            candle.close = 9.9

    def test_derived_properties(self):
        candle = make_candle(0, 1.10, 1.15, 1.05, 1.12)
        assert candle.is_bullish and not candle.is_bearish
        assert candle.range == pytest.approx(0.10)
        assert candle.body == pytest.approx(0.02)
        assert candle.body_ratio == pytest.approx(0.2)
        assert candle.upper_wick == pytest.approx(0.03)
        assert candle.lower_wick == pytest.approx(0.05)
        assert candle.midpoint == pytest.approx(0.5 * (1.15 + 1.05))

    def test_doji_has_zero_body_ratio_without_dividing_by_zero(self):
        assert make_candle(0, 1.1, 1.1, 1.1, 1.1).body_ratio == 0.0


class TestDirection:
    def test_signs(self):
        assert Direction.LONG.sign == 1
        assert Direction.SHORT.sign == -1

    def test_opposites(self):
        assert Direction.LONG.opposite is Direction.SHORT
        assert Direction.SHORT.opposite is Direction.LONG


class TestTimeframe:
    def test_parses_case_insensitively(self):
        assert Timeframe.parse("h1") is Timeframe.H1
        assert Timeframe.parse("  H4 ") is Timeframe.H4

    def test_rejects_nonsense(self):
        with pytest.raises(DataError, match="unknown timeframe"):
            Timeframe.parse("H7")

    def test_minutes(self):
        assert Timeframe.H4.minutes == 240
        assert Timeframe.D1.minutes == 1440


class TestInstruments:
    def test_jpy_pairs_use_a_bigger_pip(self):
        assert get_instrument("USDJPY").pip_size == 0.01
        assert get_instrument("EURUSD").pip_size == 0.0001

    def test_pip_distance_matches_across_conventions(self):
        assert pips_between(get_instrument("EURUSD"), 1.1000, 1.1050) == pytest.approx(50)
        assert pips_between(get_instrument("USDJPY"), 150.00, 150.50) == pytest.approx(50)

    def test_symbol_formats_are_normalised(self):
        for text in ("eurusd", "EUR/USD", "EUR_USD"):
            assert get_instrument(text).symbol == "EURUSD"

    def test_unknown_pair_gets_a_pessimistic_spread(self):
        instrument = get_instrument("EURNOK")
        assert instrument.typical_spread_pips >= 3.0

    def test_rejects_a_too_short_symbol(self):
        with pytest.raises(ConfigError):
            get_instrument("EUR")

    def test_round_trip_pip_conversion(self):
        instrument = get_instrument("EURUSD")
        assert price_from_pips(instrument, pips_between(instrument, 1.10, 1.11)) == pytest.approx(0.01)

    def test_price_comparison_uses_tolerance(self):
        instrument = get_instrument("EURUSD")
        assert same_price(instrument, 1.10000, 1.100001)
        assert not same_price(instrument, 1.1000, 1.1010)

    def test_base_and_quote(self):
        instrument = get_instrument("GBPJPY")
        assert instrument.base == "GBP"
        assert instrument.quote == "JPY"


class TestConfig:
    def test_defaults_are_valid(self):
        assert load_config(None).validate() is not None

    def test_missing_file_is_an_error(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("does/not/exist.toml")

    def test_loads_a_real_file(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text('[account]\nbalance = 5000.0\n\n[strategy]\nmin_confluence = 0.8\n')
        config = load_config(path)
        assert config.account.balance == 5000.0
        assert config.strategy.min_confluence == 0.8

    def test_unknown_key_is_rejected(self, tmp_path):
        """A typo must not leave the user trading a setting they think they set."""
        path = tmp_path / "c.toml"
        path.write_text("[account]\nbalanse = 5000.0\n")
        with pytest.raises(ConfigError, match="unknown key"):
            load_config(path)

    def test_unknown_section_is_rejected(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text("[acount]\nbalance = 5000.0\n")
        with pytest.raises(ConfigError, match="unknown top-level"):
            load_config(path)

    def test_malformed_toml_is_reported_clearly(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text("[account\nbalance = ")
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_config(path)

    @pytest.mark.parametrize(
        "section,kwargs",
        [
            ("account", {"balance": -1.0}),
            ("account", {"risk_per_trade_pct": 0.0}),
            ("account", {"risk_per_trade_pct": 50.0}),
            ("account", {"risk_per_trade_pct": 5.0, "max_concurrent_risk_pct": 1.0}),
        ],
    )
    def test_account_validation(self, section, kwargs):
        with pytest.raises(ConfigError):
            Config(account=AccountConfig(**kwargs)).validate()

    def test_strategy_ema_ordering_is_enforced(self):
        with pytest.raises(ConfigError, match="fast < slow < trend"):
            Config(strategy=StrategyConfig(ema_fast=200, ema_slow=50, ema_trend=21)).validate()

    def test_confluence_must_be_a_fraction(self):
        with pytest.raises(ConfigError, match="0-1 fraction"):
            Config(strategy=StrategyConfig(min_confluence=70.0)).validate()

    def test_data_source_is_checked(self):
        with pytest.raises(ConfigError, match="data.source"):
            Config(data=DataConfig(source="carrier-pigeon")).validate()

    def test_lookback_must_warm_the_indicators(self):
        with pytest.raises(ConfigError, match="unwarmed"):
            Config(data=DataConfig(lookback_bars=10)).validate()

    def test_target_win_rate_is_a_fraction(self):
        with pytest.raises(ConfigError, match="0-1 fraction"):
            Config(target=TargetConfig(win_rate=85.0)).validate()

    def test_api_key_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("TRADING_BOT_API_KEY", "secret-value")
        assert DataConfig().api_key == "secret-value"

    def test_api_key_is_absent_when_unset(self, monkeypatch):
        monkeypatch.delenv("TRADING_BOT_API_KEY", raising=False)
        assert DataConfig().api_key is None

    def test_shipped_config_is_valid(self):
        """The config in the repo must actually load."""
        from pathlib import Path

        shipped = Path(__file__).resolve().parents[1] / "config" / "default.toml"
        if shipped.exists():
            assert load_config(shipped) is not None


class TestSessions:
    def test_london_window(self):
        assert LONDON.contains(datetime(2024, 1, 3, 9, tzinfo=timezone.utc))
        assert not LONDON.contains(datetime(2024, 1, 3, 3, tzinfo=timezone.utc))

    def test_sydney_wraps_midnight(self):
        sydney = get_session("sydney")
        assert sydney.contains(datetime(2024, 1, 3, 23, tzinfo=timezone.utc))
        assert sydney.contains(datetime(2024, 1, 3, 2, tzinfo=timezone.utc))
        assert not sydney.contains(datetime(2024, 1, 3, 12, tzinfo=timezone.utc))

    def test_overlap_is_labelled(self):
        assert session_label(datetime(2024, 1, 3, 14, tzinfo=timezone.utc)) == "London/NY overlap"

    def test_saturday_is_closed(self):
        assert is_weekend(datetime(2024, 1, 6, 12, tzinfo=timezone.utc))

    def test_sunday_evening_is_open(self):
        assert not is_weekend(datetime(2024, 1, 7, 22, tzinfo=timezone.utc))
        assert is_weekend(datetime(2024, 1, 7, 12, tzinfo=timezone.utc))

    def test_friday_after_the_close_is_shut(self):
        assert is_weekend(datetime(2024, 1, 5, 22, tzinfo=timezone.utc))
        assert not is_weekend(datetime(2024, 1, 5, 15, tzinfo=timezone.utc))

    def test_empty_filter_admits_everything(self):
        assert in_any_session(datetime(2024, 1, 3, 3, tzinfo=timezone.utc), [])

    def test_unknown_session_name_is_ignored_not_crashing(self):
        assert not in_any_session(datetime(2024, 1, 3, 3, tzinfo=timezone.utc), ["atlantis"])

    def test_active_sessions_are_reported(self):
        names = [s.name for s in active_sessions(datetime(2024, 1, 3, 14, tzinfo=timezone.utc))]
        assert "London" in names and "New York" in names


class TestUtcNow:
    def test_is_timezone_aware(self):
        assert utc_now().tzinfo is not None
