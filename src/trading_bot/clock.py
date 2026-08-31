"""Local-time rendering for a human in one timezone trading markets in others.

Everything inside this system is UTC and stays UTC — data, structure, sessions,
the journal. That is not negotiable: the moment two components disagree about
what time a bar opened, every backtest becomes fiction.

But a person does not live in UTC. They live in one place, wake up at one hour,
and need to know that the London open lands at 08:00 *their* time and the New
York open at 13:00. This module is the only place that conversion happens, and
it happens at the edge — for display and for planning, never for a decision.

Default zone is Africa/Lagos: West Africa Time, UTC+1, and — unusually
convenient for this purpose — no daylight saving, ever. The session windows in
``sessions.py`` are fixed UTC, so a fixed-offset local zone means the local
trading day never shifts underneath the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo

from .sessions import ALL_SESSIONS, Session, get_session, is_weekend

DEFAULT_ZONE = "Africa/Lagos"

# Fallback offsets, used only when the platform has no IANA database (a slim
# container, or Windows without ``tzdata``). Lagos is UTC+1 with no DST, so for
# the default zone this fallback is not an approximation — it is exact.
_FIXED_OFFSETS: dict[str, tuple[int, str]] = {
    "africa/lagos": (1, "WAT"),
    "africa/abidjan": (0, "GMT"),
    "africa/accra": (0, "GMT"),
    "africa/nairobi": (3, "EAT"),
    "africa/johannesburg": (2, "SAST"),
    "africa/cairo": (2, "EET"),
    "utc": (0, "UTC"),
    "etc/utc": (0, "UTC"),
}


def _resolve(zone_name: str) -> tuple[tzinfo, str]:
    """Return a tzinfo for a zone name, falling back to a fixed offset.

    A missing timezone database must not take the whole tool down — the worst
    acceptable outcome is that times are shown in UTC and labelled as such.
    """
    key = zone_name.strip().lower()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(zone_name), zone_name
    except Exception:
        pass
    if key in _FIXED_OFFSETS:
        hours, label = _FIXED_OFFSETS[key]
        return timezone(timedelta(hours=hours), label), zone_name
    return timezone.utc, "UTC"


@dataclass(frozen=True)
class Clock:
    """Converts and formats UTC instants for one human in one place."""

    zone_name: str = DEFAULT_ZONE

    @property
    def tz(self) -> tzinfo:
        return _resolve(self.zone_name)[0]

    @property
    def resolved_name(self) -> str:
        """The zone actually in use, which may be UTC if the name did not resolve."""
        return _resolve(self.zone_name)[1]

    def abbrev(self, moment: datetime | None = None) -> str:
        """Short zone label — 'WAT', 'UTC' — as of a given instant."""
        local = self.local(moment or datetime.now(timezone.utc))
        return local.tzname() or self.resolved_name

    def local(self, moment: datetime) -> datetime:
        """Convert a UTC instant into local wall-clock time."""
        if moment.tzinfo is None:
            raise ValueError(f"refusing to convert naive datetime {moment!r}; UTC required")
        return moment.astimezone(self.tz)

    def local_time(self, utc_time: time) -> time:
        """Convert a fixed UTC time-of-day into local time-of-day.

        Used for session windows, which are wall-clock UTC rather than instants.
        The date is arbitrary; only the offset matters, and for a fixed-offset
        zone like WAT there is no date on which it differs.
        """
        anchor = datetime(2024, 1, 1, utc_time.hour, utc_time.minute, tzinfo=timezone.utc)
        return self.local(anchor).time()

    def stamp(self, moment: datetime) -> str:
        """'Mon 31 Aug 14:00 WAT (13:00 UTC)' — local first, UTC in brackets.

        Both are shown on purpose. Local is what the user acts on; UTC is what
        the journal, the broker platform and any support conversation will use.
        """
        local = self.local(moment)
        return (
            f"{local.strftime('%a %d %b %H:%M')} {self.abbrev(moment)} "
            f"({moment.astimezone(timezone.utc).strftime('%H:%M')} UTC)"
        )

    def short(self, moment: datetime) -> str:
        """'14:00 WAT' — for tables where the date is already established."""
        return f"{self.local(moment).strftime('%H:%M')} {self.abbrev(moment)}"

    def day(self, moment: datetime) -> str:
        """'Monday 31 August' in local time."""
        return self.local(moment).strftime("%A %d %B")


def humanise_delta(delta: timedelta) -> str:
    """'3h 20m', '45m', '2 days' — a duration a person can act on."""
    seconds = int(delta.total_seconds())
    past = seconds < 0
    seconds = abs(seconds)
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60

    if days >= 2:
        text = f"{days} days"
    elif days == 1:
        text = f"1 day {hours}h" if hours else "1 day"
    elif hours:
        text = f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    else:
        text = f"{minutes}m"
    return f"{text} ago" if past else f"in {text}"


@dataclass(frozen=True)
class SessionWindow:
    """One trading session expressed in the user's own wall clock."""

    name: str
    key: str
    local_start: time
    local_end: time
    utc_start: time
    utc_end: time

    @property
    def label(self) -> str:
        return f"{self.local_start.strftime('%H:%M')}-{self.local_end.strftime('%H:%M')}"

    @property
    def utc_label(self) -> str:
        return f"{self.utc_start.strftime('%H:%M')}-{self.utc_end.strftime('%H:%M')}"


_SESSION_KEYS = {
    "Sydney": "sydney",
    "Tokyo": "tokyo",
    "London": "london",
    "New York": "newyork",
}


def window_for(session: Session, clock: Clock) -> SessionWindow:
    """Express one session in local time."""
    return SessionWindow(
        name=session.name,
        key=_SESSION_KEYS.get(session.name, session.name.lower()),
        local_start=clock.local_time(session.start),
        local_end=clock.local_time(session.end),
        utc_start=session.start,
        utc_end=session.end,
    )


def session_windows(clock: Clock, names: list[str] | tuple[str, ...] | None = None) -> list[SessionWindow]:
    """Every session (or a named subset) in the user's local wall clock."""
    if names is None:
        sessions = list(ALL_SESSIONS)
    else:
        sessions = []
        for name in names:
            found = get_session(name)
            if found is not None and found not in sessions:
                sessions.append(found)
    return [window_for(s, clock) for s in sessions]


def next_session_open(moment: datetime, names: list[str] | tuple[str, ...]) -> tuple[str, datetime] | None:
    """When the next named session opens, as a UTC instant.

    Scans forward a week in whole hours, which is enough to clear a weekend and
    is exact because every session boundary sits on the hour. Returns ``None``
    if no name resolved to a session.
    """
    sessions = [s for s in (get_session(n) for n in names) if s is not None]
    if not sessions:
        return None

    cursor = moment.replace(minute=0, second=0, microsecond=0)
    for step in range(1, 24 * 8 + 1):
        candidate = cursor + timedelta(hours=step)
        if is_weekend(candidate):
            continue
        for session in sessions:
            # The opening hour is the one where the session is live and the hour
            # before it was not — that transition is the "open".
            if session.contains(candidate) and not session.contains(candidate - timedelta(hours=1)):
                return session.name, candidate
    return None


def trading_day_plan(clock: Clock, names: list[str] | tuple[str, ...]) -> list[str]:
    """Lines describing when to be at the screen, in the user's own time.

    This is the answer to "when do I actually look at this?" — the question a
    signal card cannot answer with a UTC timestamp alone.
    """
    lines: list[str] = []
    for window in session_windows(clock, names):
        lines.append(
            f"{window.name:<9} {window.label} {clock.abbrev()}   ({window.utc_label} UTC)"
        )
    return lines
