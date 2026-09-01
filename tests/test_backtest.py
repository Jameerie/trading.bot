"""Backtest realism.

Each test here pins one pessimistic modelling choice. Together they are the
reason a win rate from this tool is worth reading: remove any of them and the
number goes up without the strategy improving.
"""

from __future__ import annotations

import pytest

from trading_bot.backtest import run_backtest, simulate_trade, split_backtest
from trading_bot.instruments import get_instrument
from trading_bot.models import Direction, Outcome, Signal, Timeframe

from conftest import START, make_candle


def make_signal(direction=Direction.LONG, entry=1.1000, stop=1.0980, target=1.1085):
    """A hand-built signal, so simulation can be tested without the strategy."""
    return Signal(
        symbol="EURUSD",
        timeframe=Timeframe.H1,
        direction=direction,
        entry=entry,
        stop_loss=stop,
        take_profit=target,
        issued_at=START,
        score=90.0,
        max_score=122.0,
        risk_reward=4.0,
        risk_pips=20.0,
        reward_pips=85.0,
        position_lots=0.5,
        position_units=50_000,
        risk_amount=100.0,
    )


class TestRecordKeeping:
    def test_the_fill_price_is_on_the_trade(self, config, eurusd):
        """The fill is the next open moved against us; the trade must say what it was."""
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1002, 1.1010, 1.0998, 1.1005),
            make_candle(2, 1.1005, 1.1090, 1.1000, 1.1080),
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.fill_price is not None
        assert trade.fill_price > 1.1002, "spread and slippage move the fill against a long"
        assert trade.to_dict()["fill_price"] == trade.fill_price

    def test_evaluations_are_kept_only_when_asked(self, config):
        from trading_bot.data.synthetic import SyntheticSource
        from trading_bot.models import Timeframe

        candles = SyntheticSource().fetch("GBPAUD", Timeframe.H1, 900)
        plain = run_backtest(candles, "GBPAUD", config)
        kept = run_backtest(candles, "GBPAUD", config, keep_evaluations=True)
        assert plain.evaluations == []
        assert len(kept.evaluations) == len(kept.trades)
        for evaluation, trade in zip(kept.evaluations, kept.trades):
            assert evaluation.signal == trade.signal
        assert [t.r_multiple for t in kept.trades] == [t.r_multiple for t in plain.trades]


class TestTieBreaking:
    def test_a_bar_hitting_both_levels_counts_as_a_loss(self, config, eurusd):
        """The single most important assumption in the simulator.

        OHLC cannot say which level was touched first. Resolving the ambiguity in
        our favour would inflate the win rate of a 1:4 system enormously, so it
        is always resolved against us.
        """
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1000, 1.1090, 1.0975, 1.1050),  # spans stop and target
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.outcome is Outcome.LOSS

    def test_short_ties_also_lose(self, config, eurusd):
        signal = make_signal(Direction.SHORT, 1.1000, 1.1020, 1.0915)
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1000, 1.1030, 1.0910, 1.0950),
        ]
        trade = simulate_trade(candles, signal, 0, config, eurusd)
        assert trade.outcome is Outcome.LOSS


class TestFills:
    def test_entry_is_on_the_next_bar_not_the_signal_bar(self, config, eurusd):
        """You cannot trade a bar you are still inside."""
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1010, 1.1090, 1.1005, 1.1085),
            make_candle(2, 1.1085, 1.1100, 1.1080, 1.1090),
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.entry_time == candles[1].timestamp

    def test_a_signal_on_the_final_bar_cannot_fill(self, config, eurusd):
        candles = [make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000)]
        assert simulate_trade(candles, make_signal(), 0, config, eurusd) is None

    def test_fill_is_worse_than_the_quoted_entry(self, config, eurusd):
        """Spread and slippage move the fill against us, never for us.

        The signal is a textbook one: 20 pips of risk, 85 of reward. Gross that
        is 4.25R; the realised result must come in below that, because the fill
        is pushed a pip against us before the trade starts.
        """
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1000, 1.1090, 1.0999, 1.1085),
            make_candle(2, 1.1085, 1.1100, 1.1080, 1.1090),
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.outcome is Outcome.WIN
        gross_ratio = 85.0 / 20.0
        assert trade.r_multiple < gross_ratio

    def test_planned_net_ratio_is_what_the_simulator_pays(self, config, eurusd):
        """risk.py and backtest.py must agree about costs.

        ``build_stop_target`` sizes the target so the ratio is 4.0 *after* costs.
        If the simulator charges costs the same way, a clean winner returns 4.0R.
        A drift here means the two modules disagree, and the reported R would no
        longer mean what the signal card promised.
        """
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1000, 1.1090, 1.0999, 1.1085),
            make_candle(2, 1.1085, 1.1100, 1.1080, 1.1090),
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.r_multiple == pytest.approx(config.risk.min_risk_reward, abs=0.05)

    def test_gap_through_the_stop_fills_at_the_open(self, config, eurusd):
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.0900, 1.0910, 1.0890, 1.0895),  # opens well below the stop
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.outcome is Outcome.LOSS
        assert trade.r_multiple <= -1.0, "a gap must be able to cost more than 1R"


class TestOutcomes:
    def test_clean_win(self, config, eurusd):
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1000, 1.1010, 1.0999, 1.1005),
            make_candle(2, 1.1005, 1.1090, 1.1000, 1.1085),
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.outcome is Outcome.WIN
        assert trade.r_multiple > 0

    def test_clean_loss_is_about_minus_one_r(self, config, eurusd):
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1000, 1.1005, 1.0999, 1.1002),
            make_candle(2, 1.1002, 1.1004, 1.0975, 1.0978),
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.outcome is Outcome.LOSS
        assert trade.r_multiple == pytest.approx(-1.0, abs=0.01)

    def test_trade_expires_at_the_bar_limit(self, config, eurusd):
        from dataclasses import replace

        tuned = replace(config, backtest=replace(config.backtest, max_bars_in_trade=3))
        candles = [make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000)]
        candles += [make_candle(i, 1.1000, 1.1002, 1.0998, 1.1000) for i in range(1, 8)]
        trade = simulate_trade(candles, make_signal(), 0, tuned, eurusd)
        assert trade.outcome is Outcome.EXPIRED

    def test_excursions_are_recorded(self, config, eurusd):
        candles = [
            make_candle(0, 1.1000, 1.1005, 1.0995, 1.1000),
            make_candle(1, 1.1000, 1.1010, 1.0985, 1.1005),  # dips toward the stop
            make_candle(2, 1.1005, 1.1090, 1.1000, 1.1085),
        ]
        trade = simulate_trade(candles, make_signal(), 0, config, eurusd)
        assert trade.mae_r < 0
        assert trade.mfe_r > 0


class TestRunBacktest:
    def test_produces_trades_on_sample_data(self, random_series, config):
        result = run_backtest(random_series, "EURUSD", config)
        assert result.bars_tested > 0
        assert len(result.trades) == result.signals_generated

    def test_never_overlaps_positions(self, random_series, config):
        """One idea must not be counted as many overlapping trades."""
        result = run_backtest(random_series, "EURUSD", config)
        for earlier, later in zip(result.trades, result.trades[1:]):
            assert later.entry_time > earlier.entry_time
            if earlier.exit_time:
                assert later.entry_time >= earlier.exit_time

    def test_every_trade_respects_the_floor(self, random_series, config):
        for trade in run_backtest(random_series, "EURUSD", config).trades:
            assert trade.signal.risk_reward >= config.risk.min_risk_reward

    def test_is_deterministic(self, random_series, config):
        first = run_backtest(random_series, "EURUSD", config)
        second = run_backtest(random_series, "EURUSD", config)
        assert [t.to_dict() for t in first.trades] == [t.to_dict() for t in second.trades]

    def test_empty_range_is_handled(self, random_series, config):
        result = run_backtest(random_series, "EURUSD", config, start=10, end=5)
        assert result.trades == []


class TestSplit:
    def test_halves_do_not_overlap(self, random_series, config):
        in_sample, out_sample = split_backtest(random_series, "EURUSD", config, 0.7)
        if in_sample.trades and out_sample.trades:
            assert out_sample.trades[0].entry_time > in_sample.trades[-1].entry_time

    def test_rejects_a_silly_split(self, random_series, config):
        with pytest.raises(ValueError):
            split_backtest(random_series, "EURUSD", config, 0.99)
