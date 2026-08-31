"""Risk rules.

The 1:4 floor is the product. These tests exist so that a future change which
quietly relaxes it fails loudly.
"""

from __future__ import annotations

import pytest

from trading_bot.config import Config, RiskConfig, StrategyConfig, AccountConfig, load_config
from trading_bot.errors import ConfigError, RiskError
from trading_bot.instruments import get_instrument, pips_between, price_from_pips
from trading_bot.models import Direction
from trading_bot.risk import (
    atr_stop,
    build_stop_target,
    enforce_rr,
    pip_value_per_lot,
    position_size,
    structural_stop,
    total_cost_pips,
)


class TestTheFloorCannotBeConfiguredAway:
    def test_config_rejects_a_lower_floor(self):
        with pytest.raises(ConfigError, match="floor"):
            Config(risk=RiskConfig(min_risk_reward=2.0)).validate()

    def test_config_rejects_a_floor_just_below(self):
        with pytest.raises(ConfigError):
            Config(risk=RiskConfig(min_risk_reward=3.99)).validate()

    def test_exactly_four_is_allowed(self):
        assert Config(risk=RiskConfig(min_risk_reward=4.0)).validate() is not None

    def test_a_higher_floor_is_allowed(self):
        assert Config(risk=RiskConfig(min_risk_reward=6.0)).validate() is not None


class TestStopTarget:
    def test_target_clears_the_floor_net_of_costs(self, config, eurusd):
        setup = build_stop_target(Direction.LONG, 1.1000, 1.0980, eurusd, config)
        assert setup.risk_reward >= config.risk.min_risk_reward
        # The gross ratio must exceed the net one, or costs were not charged.
        assert setup.gross_risk_reward > setup.risk_reward

    def test_short_is_symmetric(self, config, eurusd):
        long_setup = build_stop_target(Direction.LONG, 1.1000, 1.0980, eurusd, config)
        short_setup = build_stop_target(Direction.SHORT, 1.1000, 1.1020, eurusd, config)
        assert long_setup.risk_pips == pytest.approx(short_setup.risk_pips)
        assert long_setup.reward_pips == pytest.approx(short_setup.reward_pips)

    def test_stop_on_the_wrong_side_is_rejected(self, config, eurusd):
        with pytest.raises(RiskError, match="wrong side"):
            build_stop_target(Direction.LONG, 1.1000, 1.1020, eurusd, config)
        with pytest.raises(RiskError, match="wrong side"):
            build_stop_target(Direction.SHORT, 1.1000, 1.0980, eurusd, config)

    def test_a_generous_structural_target_is_taken(self, config, eurusd):
        """A target beyond the floor should be used, not trimmed back to 4R."""
        far = 1.1500
        setup = build_stop_target(Direction.LONG, 1.1000, 1.0980, eurusd, config, target_level=far)
        assert setup.take_profit == pytest.approx(far)
        assert setup.risk_reward > config.risk.min_risk_reward

    def test_a_short_structural_target_does_not_shrink_the_trade(self, config, eurusd):
        """A near target must not drag the ratio below the floor."""
        near = 1.1010
        setup = build_stop_target(Direction.LONG, 1.1000, 1.0980, eurusd, config, target_level=near)
        assert setup.take_profit > near
        assert setup.risk_reward >= config.risk.min_risk_reward

    def test_jpy_pip_size_is_handled(self, config, usdjpy):
        setup = build_stop_target(Direction.LONG, 150.00, 149.70, usdjpy, config)
        assert setup.risk_pips == pytest.approx(30.0, abs=0.01)
        assert setup.risk_reward >= config.risk.min_risk_reward


class TestEnforceRr:
    def test_accepts_a_qualifying_setup(self, config, eurusd):
        enforce_rr(build_stop_target(Direction.LONG, 1.1000, 1.0980, eurusd, config), config)

    def test_rejects_a_short_ratio(self, config, eurusd):
        from trading_bot.risk import StopTarget

        bad = StopTarget(1.1000, 1.0980, 1.1050, 20.0, 50.0, 2.5, 2.5)
        with pytest.raises(RiskError, match="below the"):
            enforce_rr(bad, config)

    def test_rejects_an_absurd_ratio(self, config):
        from trading_bot.risk import StopTarget

        silly = StopTarget(1.1000, 1.0980, 1.6000, 20.0, 5000.0, 250.0, 250.0)
        with pytest.raises(RiskError, match="ceiling"):
            enforce_rr(silly, config)


class TestStopPlacement:
    def test_stop_sits_beyond_the_structure(self, config, eurusd):
        stop = structural_stop(Direction.LONG, 1.1000, 1.0990, 0.0010, eurusd, config)
        assert stop < 1.0990, "a long stop must sit below the swing it protects"

    def test_short_stop_sits_above(self, config, eurusd):
        stop = structural_stop(Direction.SHORT, 1.1000, 1.1010, 0.0010, eurusd, config)
        assert stop > 1.1010

    def test_distance_is_clamped_to_the_maximum(self, config, eurusd):
        stop = structural_stop(Direction.LONG, 1.1000, 1.0000, 0.0010, eurusd, config)
        assert pips_between(eurusd, 1.1000, stop) <= config.risk.max_stop_pips + 0.01

    def test_distance_is_clamped_to_the_minimum(self, config, eurusd):
        stop = structural_stop(Direction.LONG, 1.1000, 1.09999, 0.0000001, eurusd, config)
        assert pips_between(eurusd, 1.1000, stop) >= config.risk.min_stop_pips - 0.01

    def test_atr_stop_respects_bounds(self, config, eurusd):
        stop = atr_stop(Direction.LONG, 1.1000, 0.0500, eurusd, config)
        assert pips_between(eurusd, 1.1000, stop) <= config.risk.max_stop_pips + 0.01


class TestPositionSizing:
    def test_loss_at_stop_matches_configured_risk(self, config, eurusd):
        size = position_size(eurusd, 1.1000, 1.0980, config)
        budget = config.account.balance * config.account.risk_per_trade_pct / 100
        assert size.risk_amount <= budget + 0.01

    def test_never_rounds_risk_upward(self, config, eurusd):
        """Rounding lots up would breach the risk cap, so sizing rounds down."""
        budget = config.account.balance * config.account.risk_per_trade_pct / 100
        for stop in (1.0980, 1.09835, 1.0977, 1.09912):
            size = position_size(eurusd, 1.1000, stop, config)
            assert size.risk_amount <= budget + 0.01

    def test_wider_stop_gives_a_smaller_position(self, config, eurusd):
        tight = position_size(eurusd, 1.1000, 1.0990, config)
        wide = position_size(eurusd, 1.1000, 1.0950, config)
        assert wide.lots < tight.lots

    def test_zero_stop_is_rejected(self, config, eurusd):
        with pytest.raises(RiskError):
            position_size(eurusd, 1.1000, 1.1000, config)

    def test_doubling_the_balance_doubles_the_size(self, config, eurusd):
        base = position_size(eurusd, 1.1000, 1.0980, config)
        bigger = position_size(
            eurusd, 1.1000, 1.0980,
            Config(account=AccountConfig(balance=config.account.balance * 2)).validate(),
        )
        assert bigger.lots == pytest.approx(base.lots * 2, rel=0.02)


class TestPipValue:
    def test_quote_currency_account_is_exact(self, eurusd):
        value, approximate = pip_value_per_lot(eurusd, 1.1000, "USD")
        assert value == pytest.approx(10.0)
        assert not approximate

    def test_base_currency_account_converts_at_price(self, usdjpy):
        value, approximate = pip_value_per_lot(usdjpy, 150.0, "USD")
        assert value == pytest.approx(1000.0 / 150.0)
        assert not approximate

    def test_cross_pair_is_flagged_as_approximate(self):
        instrument = get_instrument("EURGBP")
        _, approximate = pip_value_per_lot(instrument, 0.85, "USD")
        assert approximate, "an unconvertible cross must be flagged, not silently guessed"


class TestCosts:
    def test_costs_include_spread_and_slippage(self, config, eurusd):
        expected = eurusd.typical_spread_pips + config.backtest.slippage_pips
        assert total_cost_pips(eurusd, config) == pytest.approx(expected)

    def test_wider_spread_lowers_the_net_ratio(self, config, eurusd):
        from dataclasses import replace

        cheap = replace(config, backtest=replace(config.backtest, spread_pips=0.1))
        dear = replace(config, backtest=replace(config.backtest, spread_pips=5.0))
        a = build_stop_target(Direction.LONG, 1.1000, 1.0980, eurusd, cheap, target_level=1.1200)
        b = build_stop_target(Direction.LONG, 1.1000, 1.0980, eurusd, dear, target_level=1.1200)
        assert b.risk_reward < a.risk_reward
