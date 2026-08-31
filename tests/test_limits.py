"""Account-level loss limits.

These answer the question the rest of the package does not ask: not "is this
setup good" but "should you be trading at all right now". They are advisory, like
everything else here, so the tests check that a breach *warns* and never
suppresses the advice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.config import Config, LimitsConfig, load_config
from trading_bot.errors import ConfigError
from trading_bot.journal import JournalEntry
from trading_bot.limits import DAILY_LOSS, MAX_DRAWDOWN, evaluate_limits

NOON = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def entry(r: float | None, when: datetime = NOON) -> JournalEntry:
    """A journalled signal, closed at ``r`` unless ``r`` is None."""
    return JournalEntry(
        recorded_at=when,
        signal={"symbol": "EURUSD", "issued_at": when.isoformat()},
        outcome=None if r is None else ("win" if r > 0 else "loss"),
        r_multiple=r,
        closed_at=None if r is None else when,
    )


@pytest.fixture
def cfg() -> Config:
    """1% risk per trade, 3% daily limit, 10% drawdown limit."""
    return Config().validate()


class TestDailyLoss:
    def test_a_clean_slate_is_not_breached(self, cfg):
        status = evaluate_limits([], cfg, NOON)
        assert not status.breached
        assert status.banner() == ""

    def test_three_full_losses_at_one_percent_reach_the_three_percent_limit(self, cfg):
        status = evaluate_limits([entry(-1.0) for _ in range(3)], cfg, NOON)
        assert status.daily_loss_pct == pytest.approx(3.0)
        assert status.breached
        assert status.breaches[0].name == DAILY_LOSS

    def test_the_limit_is_inclusive(self, cfg):
        """Landing exactly on the limit is reaching it, not staying under it."""
        assert evaluate_limits([entry(-3.0)], cfg, NOON).breached

    def test_just_under_the_limit_still_allows_trading(self, cfg):
        status = evaluate_limits([entry(-2.9)], cfg, NOON)
        assert not status.breached
        assert status.headroom_pct() == pytest.approx(0.1)

    def test_a_winning_day_is_not_headroom_to_spend(self, cfg):
        """+4R then -1R is a good day. It does not license a bigger loss later."""
        status = evaluate_limits([entry(4.0), entry(-1.0)], cfg, NOON)
        assert status.daily_loss_pct == 0.0
        assert status.headroom_pct() == pytest.approx(3.0)

    def test_yesterdays_losses_do_not_count_against_today(self, cfg):
        yesterday = NOON - timedelta(days=1)
        status = evaluate_limits([entry(-1.0, yesterday) for _ in range(3)], cfg, NOON)
        assert status.daily_loss_pct == 0.0

    def test_risk_per_trade_scales_the_limit(self):
        """At 2% a trade, two losses breach a 3% daily limit; at 1% they do not."""
        from dataclasses import replace

        base = Config()
        loose = replace(base, account=replace(base.account, risk_per_trade_pct=1.0)).validate()
        tight = replace(base, account=replace(base.account, risk_per_trade_pct=2.0)).validate()
        two = [entry(-1.0), entry(-1.0)]
        assert not evaluate_limits(two, loose, NOON).breached
        assert evaluate_limits(two, tight, NOON).breached


class TestDrawdown:
    def test_peak_to_trough_across_days_triggers_the_limit(self, cfg):
        old = NOON - timedelta(days=20)
        status = evaluate_limits([entry(-1.0, old) for _ in range(12)], cfg, NOON)
        assert status.drawdown_pct == pytest.approx(12.0)
        assert status.breached
        assert status.breaches[0].name == MAX_DRAWDOWN
        assert status.daily_loss_pct == 0.0

    def test_drawdown_is_measured_from_the_peak_not_the_start(self, cfg):
        """Up 8R then down 11R is an 11% drawdown, though the account is still up."""
        days = [NOON - timedelta(days=n) for n in range(4, 0, -1)]
        entries = [entry(8.0, days[0])] + [entry(-11.0, days[1])]
        status = evaluate_limits(entries, cfg, NOON)
        assert status.drawdown_pct == pytest.approx(11.0)
        assert status.breached

    def test_recovery_does_not_erase_the_breach(self, cfg):
        """A drawdown that happened, happened. This is a record, not a mood."""
        days = [NOON - timedelta(days=n) for n in range(3, 0, -1)]
        entries = [entry(-11.0, days[0]), entry(20.0, days[1])]
        assert evaluate_limits(entries, cfg, NOON).breached


class TestWhatIsCounted:
    def test_open_signals_are_ignored(self, cfg):
        """An open trade has no outcome yet; guessing one would be inventing data."""
        status = evaluate_limits([entry(None) for _ in range(9)], cfg, NOON)
        assert status.closed_trades == 0
        assert not status.breached

    def test_only_journalled_trades_are_visible(self, cfg):
        """The documented blind spot, pinned so nobody mistakes it for protection.

        A trade taken and never recorded cannot be seen here, so these limits are
        a floor on real losses and never a ceiling.
        """
        recorded = evaluate_limits([entry(-1.0)], cfg, NOON)
        assert recorded.daily_loss_pct == pytest.approx(1.0)
        assert evaluate_limits([], cfg, NOON).daily_loss_pct == 0.0

    def test_disabling_the_limits_reports_but_never_breaches(self, cfg):
        from dataclasses import replace

        off = replace(cfg, limits=LimitsConfig(enabled=False)).validate()
        status = evaluate_limits([entry(-1.0) for _ in range(20)], off, NOON)
        assert status.daily_loss_pct == pytest.approx(20.0)
        assert not status.breached
        assert status.banner() == ""

    def test_a_naive_timestamp_is_rejected(self, cfg):
        with pytest.raises(ValueError, match="timezone-aware"):
            evaluate_limits([], cfg, datetime(2026, 8, 30, 12, 0))


class TestAdvisoryOnly:
    def test_the_banner_says_advice_continues(self, cfg):
        """The product rule, asserted: a breach warns, it does not withhold."""
        banner = evaluate_limits([entry(-1.0) for _ in range(3)], cfg, NOON).banner()
        assert "Advice continues" in banner
        assert "3.00%" in banner

    def test_status_carries_both_numbers_even_when_clean(self, cfg):
        status = evaluate_limits([entry(-1.0)], cfg, NOON)
        assert status.daily_limit_pct == 3.0
        assert status.drawdown_limit_pct == 10.0
        assert status.closed_trades == 1


class TestLimitsConfig:
    def test_defaults_load_from_the_shipped_config(self):
        limits = load_config("config/default.toml").limits
        assert limits.enabled is True
        assert limits.daily_loss_pct == 3.0
        assert limits.max_drawdown_pct == 10.0

    def test_a_daily_limit_above_the_drawdown_limit_is_rejected(self):
        """Otherwise one day could legally blow through the account-wide limit."""
        with pytest.raises(ConfigError, match="max_drawdown_pct"):
            LimitsConfig(daily_loss_pct=12.0, max_drawdown_pct=10.0).validate()

    @pytest.mark.parametrize("pct", [0.0, -1.0, 101.0])
    def test_out_of_range_percentages_are_rejected(self, pct):
        with pytest.raises(ConfigError):
            LimitsConfig(daily_loss_pct=pct).validate()

    def test_an_unknown_key_is_an_error(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text("[limits]\ndaily_los_pct = 3.0\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="daily_los_pct"):
            load_config(path)
