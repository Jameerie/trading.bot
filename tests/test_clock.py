"""Local-time rendering.

The decision path is UTC and must stay UTC. These tests pin that boundary: the
clock converts for display and for planning, and nothing it does can reach back
into a session check or a candle timestamp.

The other half is the reason the module exists — a user in Lagos needs to know
that "the London open" means 08:00 on their own wall clock, not 07:00.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from trading_bot.clock import (
    DEFAULT_ZONE,
    Clock,
    humanise_delta,
    next_session_open,
    session_windows,
    trading_day_plan,
    window_for,
)
from trading_bot.sessions import LONDON, NEW_YORK, TOKYO


class TestClock:
    def test_lagos_is_one_hour_ahead_of_utc(self):
        """WAT is UTC+1 with no daylight saving, so this holds on any date."""
        clock = Clock(DEFAULT_ZONE)
        for month in (1, 4, 7, 10):
            moment = datetime(2024, month, 15, 12, 0, tzinfo=timezone.utc)
            assert clock.local(moment).hour == 13

    def test_abbreviation_is_wat(self):
        assert Clock(DEFAULT_ZONE).abbrev() in ("WAT", "Africa/Lagos")

    def test_stamp_shows_local_first_and_utc_in_brackets(self):
        moment = datetime(2024, 3, 4, 13, 0, tzinfo=timezone.utc)
        text = Clock(DEFAULT_ZONE).stamp(moment)
        assert "14:00" in text, "local time leads"
        assert "(13:00 UTC)" in text, "UTC stays visible for the journal and the broker"

    def test_naive_datetimes_are_refused(self):
        """A naive timestamp here would mean one leaked past the data boundary."""
        with pytest.raises(ValueError, match="naive"):
            Clock().local(datetime(2024, 1, 1, 12, 0))

    def test_an_unknown_zone_falls_back_to_utc_rather_than_crashing(self):
        clock = Clock("Mars/Olympus_Mons")
        moment = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert clock.local(moment).hour == 12
        assert clock.resolved_name == "UTC"

    def test_utc_zone_is_a_no_op(self):
        moment = datetime(2024, 6, 1, 9, 30, tzinfo=timezone.utc)
        assert Clock("UTC").local(moment).hour == 9

    def test_day_renders_in_local_time(self):
        # 23:30 UTC on the 1st is 00:30 on the 2nd in Lagos.
        moment = datetime(2024, 5, 1, 23, 30, tzinfo=timezone.utc)
        assert "02 May" in Clock(DEFAULT_ZONE).day(moment)


class TestSessionWindows:
    def test_london_opens_at_eight_in_lagos(self):
        window = window_for(LONDON, Clock(DEFAULT_ZONE))
        assert window.local_start == time(8, 0)
        assert window.local_end == time(17, 0)
        assert window.utc_label == "07:00-16:00"

    def test_new_york_and_tokyo_shift_by_the_same_hour(self):
        clock = Clock(DEFAULT_ZONE)
        assert window_for(NEW_YORK, clock).local_start == time(13, 0)
        assert window_for(TOKYO, clock).local_start == time(1, 0)

    def test_named_subset_is_returned_in_order(self):
        windows = session_windows(Clock(), ["london", "newyork"])
        assert [w.name for w in windows] == ["London", "New York"]

    def test_unknown_session_names_are_dropped_not_fatal(self):
        assert session_windows(Clock(), ["london", "atlantis"])[0].name == "London"

    def test_plan_lines_carry_both_clocks(self):
        lines = trading_day_plan(Clock(DEFAULT_ZONE), ["london"])
        assert "08:00-17:00" in lines[0]
        assert "(07:00-16:00 UTC)" in lines[0]


class TestNextSessionOpen:
    def test_finds_the_next_london_open(self):
        # Wednesday 03:00 UTC — London opens at 07:00 the same day.
        found = next_session_open(datetime(2024, 5, 1, 3, 0, tzinfo=timezone.utc), ["london"])
        assert found is not None
        name, when = found
        assert name == "London"
        assert when == datetime(2024, 5, 1, 7, 0, tzinfo=timezone.utc)

    def test_skips_the_weekend(self):
        """Friday evening: the next London open is Monday, not Saturday."""
        found = next_session_open(
            datetime(2024, 5, 3, 20, 0, tzinfo=timezone.utc), ["london"]
        )
        assert found is not None
        _, when = found
        assert when.weekday() == 0, "Monday"
        assert when.hour == 7

    def test_no_known_session_returns_none(self):
        assert next_session_open(datetime(2024, 5, 1, tzinfo=timezone.utc), ["narnia"]) is None


class TestHumaniseDelta:
    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(minutes=45), "in 45m"),
            (timedelta(hours=3, minutes=20), "in 3h 20m"),
            (timedelta(hours=2), "in 2h"),
            (timedelta(days=1, hours=2), "in 1 day 2h"),
            (timedelta(days=5), "in 5 days"),
            (timedelta(minutes=-30), "30m ago"),
        ],
    )
    def test_reads_like_a_person_wrote_it(self, delta, expected):
        assert humanise_delta(delta) == expected
