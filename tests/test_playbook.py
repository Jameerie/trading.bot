"""The handholding.

This is rendering, so most of it is checked by reading it. What is worth pinning
is the content that would be *wrong* rather than ugly:

* a stale bar must be marked unactionable, because everything else on the card
  assumes price is still near the entry;
* the management advice must say the measured numbers assume set-and-forget,
  since a reader who trails their stop is trading something nobody measured;
* the near-miss explanation must name what is missing, not just how short the
  score fell — that is the whole difference from "no setup on H1".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.clock import Clock
from trading_bot.instruments import get_instrument
from trading_bot.models import Direction, Signal, Timeframe
from trading_bot.playbook import (
    CHECK_GUIDE,
    aftercare,
    contingencies,
    daily_briefing,
    explain_no_signal,
    invalidation_plan,
    management_plan,
    order_ticket,
    timing_plan,
    wrap,
)
from trading_bot.strategy.confluence import ConfluenceResult
from trading_bot.strategy.trend_pullback import DEFAULT_CHECKS
from trading_bot.models import Reason

NOW = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def signal():
    return Signal(
        symbol="EURUSD",
        timeframe=Timeframe.H1,
        direction=Direction.LONG,
        entry=1.0850,
        stop_loss=1.0830,
        take_profit=1.0940,
        issued_at=NOW,
        score=95.0,
        max_score=122.0,
        risk_reward=4.5,
        risk_pips=20.0,
        reward_pips=90.0,
        position_lots=0.5,
        position_units=50_000,
        risk_amount=100.0,
        account_currency="USD",
    )


class TestOrderTicket:
    def test_it_names_every_field_a_broker_asks_for(self, signal, config):
        text = "\n".join(order_ticket(signal, get_instrument("EURUSD"), config))
        for field in ("Symbol", "Order type", "Price", "Volume", "Stop loss", "Take profit"):
            assert field in text

    def test_it_asks_for_a_pending_order_not_a_market_one(self, signal, config):
        text = "\n".join(order_ticket(signal, get_instrument("EURUSD"), config))
        assert "Buy Limit" in text
        assert "not a market order" in text

    def test_it_guards_the_decimal_point(self, signal, config):
        """The lot size typo is the most expensive mistake available here."""
        text = "\n".join(order_ticket(signal, get_instrument("EURUSD"), config))
        assert "0.50 lots and not 50" in text
        assert "round DOWN" in text

    def test_a_short_reverses_the_order_type(self, signal, config):
        short = Signal(**{**signal.__dict__, "direction": Direction.SHORT})
        text = "\n".join(order_ticket(short, get_instrument("EURUSD"), config))
        assert "Sell Limit" in text


class TestTiming:
    def test_it_gives_the_pair_s_liquid_hours_in_local_time(self, signal, config):
        text = "\n".join(timing_plan(signal, get_instrument("EURUSD"), Clock("Africa/Lagos"), NOW))
        assert "WAT" in text
        assert "London" in text and "New York" in text

    def test_an_asian_cross_points_at_the_asian_sessions(self, signal, config):
        audjpy = Signal(**{**signal.__dict__, "symbol": "AUDJPY"})
        text = "\n".join(timing_plan(audjpy, get_instrument("AUDJPY"), Clock(), NOW))
        assert "Tokyo" in text

    def test_a_stale_bar_is_marked_unactionable(self, signal, config):
        """A card built from old candles is a history lesson, not a trade."""
        later = NOW + timedelta(days=3)
        text = "\n".join(timing_plan(signal, get_instrument("EURUSD"), Clock(), later))
        assert "NOT ACTIONABLE" in text
        assert "Re-scan on fresh data" in text

    def test_a_fresh_bar_carries_no_such_warning(self, signal, config):
        text = "\n".join(
            timing_plan(signal, get_instrument("EURUSD"), Clock(), NOW + timedelta(hours=1))
        )
        assert "NOT ACTIONABLE" not in text


class TestManagementAndInvalidation:
    def test_it_says_the_measured_result_assumes_set_and_forget(self, signal, config):
        text = "\n".join(management_plan(signal, config, Clock()))
        assert "Do nothing" in text
        assert "measured" in text
        assert "no measured record" in text, "moving the stop invalidates the numbers"

    def test_it_forbids_the_two_ways_to_ruin(self, signal, config):
        text = "\n".join(management_plan(signal, config, Clock()))
        assert "Widening the stop" in text
        assert "Adding to a loser" in text

    def test_invalidation_covers_before_and_after_the_fill(self, signal, config):
        text = "\n".join(invalidation_plan(signal, get_instrument("EURUSD"), config))
        assert "Before it fills" in text
        assert "After it fills" in text
        assert "do not chase" in text

    def test_a_stop_out_is_framed_as_the_plan_working(self, signal, config):
        text = "\n".join(invalidation_plan(signal, get_instrument("EURUSD"), config))
        assert "was not a mistake" in text


class TestContingenciesAndAftercare:
    def test_it_answers_the_missed_entry(self, signal, config):
        text = "\n".join(contingencies(signal, get_instrument("EURUSD"), config))
        assert "already past" in text
        assert "Skip it" in text

    def test_it_admits_it_reads_no_news_calendar(self, signal, config):
        text = "\n".join(contingencies(signal, get_instrument("EURUSD"), config))
        assert "does not read a news calendar" in text

    def test_aftercare_gives_a_runnable_command(self, signal, config):
        text = "\n".join(aftercare(signal, config))
        assert "journal --close" in text
        assert signal.symbol in text
        assert "forecast --resolve" in text


class TestNoSignalExplanation:
    def _scored(self, fired_codes):
        by_code = {c.code: c for c in DEFAULT_CHECKS}
        reasons = tuple(
            Reason(code, f"{code} fired", by_code[code].weight) for code in fired_codes
        )
        missing = tuple(c.code for c in DEFAULT_CHECKS if c.code not in fired_codes)
        return ConfluenceResult(
            score=sum(r.weight for r in reasons),
            max_score=sum(c.weight for c in DEFAULT_CHECKS),
            reasons=reasons,
            missing=missing,
        )

    def test_it_names_what_is_missing_and_what_would_change_it(self, config):
        scored = self._scored(["HTF_ALIGN", "ADX", "SESSION"])
        lines = explain_no_signal(
            "EURUSD", "H1", scored.fraction, scored, Direction.LONG, config
        )
        text = "\n".join(lines)
        assert "Already true" in text
        assert "Still needed" in text
        assert "close beyond the last swing high" in text, "the guidance, not the code"

    def test_missing_checks_are_listed_heaviest_first(self, config):
        """Order is the advice. The +20 the setup lacks matters more than the +6."""
        import re

        scored = self._scored(["SESSION"])
        lines = explain_no_signal(
            "EURUSD", "H1", scored.fraction, scored, Direction.LONG, config
        )
        start = lines.index("    Still needed — this is what to watch for:")
        weights = [int(m) for m in re.findall(r"\(\+(\d+)\):", "\n".join(lines[start:]))]
        assert weights, "the block must list what is missing, with its weight"
        assert weights == sorted(weights, reverse=True)
        assert weights[0] == 20, "HTF alignment is the heaviest check there is"

    def test_a_near_miss_becomes_a_watchlist_item(self, config):
        codes = ["HTF_ALIGN", "BOS", "STRUCTURE", "EMA_STACK", "PULLBACK", "ADX", "EMA_SLOPE"]
        scored = self._scored(codes)
        text = "\n".join(
            explain_no_signal("EURUSD", "H1", scored.fraction, scored, Direction.LONG, config)
        )
        assert "watchlist" in text

    def test_a_dead_market_is_told_to_be_left_alone(self, config):
        scored = self._scored(["SESSION"])
        text = "\n".join(
            explain_no_signal("EURUSD", "H1", scored.fraction, scored, Direction.LONG, config)
        )
        assert "leave EURUSD" in text

    def test_an_unscorable_bar_says_so_plainly(self, config):
        text = "\n".join(explain_no_signal("EURUSD", "H1", None, None, None, config))
        assert "Nothing to score" in text

    def test_every_check_has_guidance_written_for_it(self):
        """A missing entry would print a bare code at the reader."""
        for check in DEFAULT_CHECKS:
            assert check.code in CHECK_GUIDE, f"no guidance for {check.code}"
            title, detail = CHECK_GUIDE[check.code]
            assert title and detail


class TestBriefing:
    def test_it_leads_with_the_local_date_and_time(self, config):
        lines = daily_briefing(config, Clock("Africa/Lagos"), NOW)
        assert "Wednesday 01 May" in lines[0]
        assert "WAT" in lines[0]

    def test_it_prints_the_trading_windows_in_local_time(self, config):
        text = "\n".join(daily_briefing(config, Clock("Africa/Lagos"), NOW))
        assert "London 08:00-17:00" in text

    def test_a_closed_market_is_stated_up_front(self, config):
        saturday = datetime(2024, 5, 4, 12, 0, tzinfo=timezone.utc)
        text = "\n".join(daily_briefing(config, Clock(), saturday))
        assert "closed" in text


class TestWrap:
    def test_long_advice_is_wrapped_to_the_card(self):
        lines = wrap("word " * 60, indent="    ")
        assert all(len(line) <= 78 for line in lines)
        assert len(lines) > 1

    def test_a_hanging_indent_is_preserved(self):
        lines = wrap("word " * 40, indent="        ", first="      - ")
        assert lines[0].startswith("      - ")
        assert all(line.startswith("        ") for line in lines[1:])
