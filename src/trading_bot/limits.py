"""Account-level loss limits.

The rest of this package judges one setup at a time: is the structure there, is
the ratio good enough, how much should be risked. None of that asks the question
that actually empties accounts — *should you be trading right now at all?*

A proprietary trading firm answers it with two hard numbers: how much may be lost
in a day, and how far below the peak the account may ever sit. Both are worth
keeping on your own money, because the real danger after four losses is not that
the fifth setup is poor. It is that a human who has just lost money starts taking
setups they would have skipped that morning.

**This module warns. It does not act.** It cannot cancel a signal, resize a
position, or stop anything, because nothing in this program can. A breach adds a
line to the signal card, and the human decides what to do about it — which is the
whole design of the tool.

Two honest limitations, stated here rather than discovered later:

- **Only journalled trades count.** A trade you took without recording it is
  invisible here, so the numbers are a floor on your real losses, never a
  ceiling. The limit cannot protect you from what you did not tell it.
- **Losses are measured in R, then scaled by the configured risk per trade.** If
  you actually risked more than ``account.risk_per_trade_pct`` on a trade, the
  real drawdown is larger than what this reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .journal import JournalEntry
from .metrics import max_drawdown

DAILY_LOSS = "daily loss"
MAX_DRAWDOWN = "max drawdown"


@dataclass(frozen=True)
class Breach:
    """One limit that has been crossed."""

    name: str
    limit_pct: float
    actual_pct: float

    def __str__(self) -> str:
        return f"{self.name} {self.actual_pct:.2f}% of {self.limit_pct:.2f}% limit"


@dataclass(frozen=True)
class LimitStatus:
    """Where the account stands against its limits."""

    daily_loss_pct: float
    drawdown_pct: float
    daily_limit_pct: float
    drawdown_limit_pct: float
    closed_trades: int
    breaches: tuple[Breach, ...]
    enabled: bool = True

    @property
    def breached(self) -> bool:
        return bool(self.breaches)

    def headroom_pct(self) -> float:
        """How much more may be lost today before the daily limit is reached."""
        return max(0.0, self.daily_limit_pct - self.daily_loss_pct)

    def banner(self) -> str:
        """One line for the signal card, empty when nothing is wrong."""
        if not self.breached:
            return ""
        crossed = "; ".join(str(b) for b in self.breaches)
        return (
            f"RISK LIMIT REACHED - {crossed}. "
            f"Advice continues, but the rules you set say stop for now."
        )


def evaluate_limits(
    entries: list[JournalEntry], config: Config, now: datetime | None = None
) -> LimitStatus:
    """Measure journalled losses against the configured limits.

    Losses are summed in R and scaled by ``account.risk_per_trade_pct``, so a
    2R losing day at 1% risk per trade reads as 2% of the account. That keeps the
    limits meaningful without this module needing to track cash balances, which
    it has no reliable way to know.

    The daily figure uses the UTC calendar day, matching the rest of the package.
    """
    limits = config.limits
    risk_pct = config.account.risk_per_trade_pct
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware UTC")

    closed = [e for e in entries if e.r_multiple is not None and e.closed_at is not None]
    closed.sort(key=lambda e: e.closed_at)

    today = moment.astimezone(timezone.utc).date()
    daily_r = sum(
        e.r_multiple for e in closed if e.closed_at.astimezone(timezone.utc).date() == today
    )
    # Only a losing day counts against the limit; a winning day is not headroom
    # to be spent, it is just a winning day.
    daily_loss_pct = max(0.0, -daily_r) * risk_pct
    drawdown_pct = max_drawdown([e.r_multiple for e in closed]) * risk_pct

    breaches: list[Breach] = []
    if limits.enabled:
        if daily_loss_pct >= limits.daily_loss_pct:
            breaches.append(Breach(DAILY_LOSS, limits.daily_loss_pct, daily_loss_pct))
        if drawdown_pct >= limits.max_drawdown_pct:
            breaches.append(Breach(MAX_DRAWDOWN, limits.max_drawdown_pct, drawdown_pct))

    return LimitStatus(
        daily_loss_pct=round(daily_loss_pct, 4),
        drawdown_pct=round(drawdown_pct, 4),
        daily_limit_pct=limits.daily_loss_pct,
        drawdown_limit_pct=limits.max_drawdown_pct,
        closed_trades=len(closed),
        breaches=tuple(breaches),
        enabled=limits.enabled,
    )
