"""Trading session windows.

Session matters for a selective strategy: the London and New York opens carry
the volume that makes a 1:4 target reachable within a sensible holding period,
while the late-Asia lull produces the chop that stops such targets out.

All windows are in UTC. Broker-local time is deliberately not supported —
it varies by broker and would make results irreproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class Session:
    """A named intraday window, half-open: start <= t < end."""

    name: str
    start: time
    end: time

    def contains(self, moment: datetime) -> bool:
        """Whether a UTC timestamp falls inside this window."""
        t = moment.timetz().replace(tzinfo=None)
        if self.start <= self.end:
            return self.start <= t < self.end
        # Window wraps midnight (Sydney), so either side counts.
        return t >= self.start or t < self.end


# Approximate centres of liquidity, in UTC, ignoring DST shifts. The half-hour
# padding on London/NY covers the DST drift rather than pretending precision.
SYDNEY = Session("Sydney", time(21, 0), time(6, 0))
TOKYO = Session("Tokyo", time(0, 0), time(9, 0))
LONDON = Session("London", time(7, 0), time(16, 0))
NEW_YORK = Session("New York", time(12, 0), time(21, 0))
LONDON_NY_OVERLAP = Session("London/NY overlap", time(12, 0), time(16, 0))

ALL_SESSIONS = (SYDNEY, TOKYO, LONDON, NEW_YORK)

_BY_NAME = {
    "sydney": SYDNEY,
    "tokyo": TOKYO,
    "asia": TOKYO,
    "london": LONDON,
    "newyork": NEW_YORK,
    "new_york": NEW_YORK,
    "ny": NEW_YORK,
    "overlap": LONDON_NY_OVERLAP,
}


def get_session(name: str) -> Session | None:
    """Look up a session by a forgiving name. Returns None if unknown."""
    return _BY_NAME.get(name.strip().lower().replace(" ", "").replace("-", ""))


def active_sessions(moment: datetime) -> list[Session]:
    """Which sessions are open at a given UTC time."""
    return [s for s in ALL_SESSIONS if s.contains(moment)]


def in_any_session(moment: datetime, names: list[str]) -> bool:
    """Whether the moment falls in any of the named sessions.

    An empty list means 'no session filter', which is treated as always true so
    that callers do not have to special-case an unfiltered config.
    """
    if not names:
        return True
    for name in names:
        session = get_session(name)
        if session is not None and session.contains(moment):
            return True
    return False


def is_weekend(moment: datetime) -> bool:
    """Whether the FX market is shut.

    The week runs from Sunday 21:00 UTC to Friday 21:00 UTC. Bars outside that
    are broker artefacts and should not generate signals.
    """
    weekday = moment.weekday()  # Monday = 0
    if weekday == 5:  # Saturday
        return True
    if weekday == 6:  # Sunday, before the Sydney open
        return moment.hour < 21
    if weekday == 4:  # Friday, after the New York close
        return moment.hour >= 21
    return False


def session_label(moment: datetime) -> str:
    """Human-readable session description for signal cards."""
    if is_weekend(moment):
        return "market closed"
    names = [s.name for s in active_sessions(moment)]
    if not names:
        return "off-session"
    if LONDON_NY_OVERLAP.contains(moment):
        return "London/NY overlap"
    return " + ".join(names)
