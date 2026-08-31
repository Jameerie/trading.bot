"""Currency exposure across the signals on the table at one moment.

Scanning three pairs, this module would have little to say. Scanning sixty, it
is the difference between a diversified book and a single enormous bet wearing
several hats.

Long EURUSD, long EURJPY, short EURGBP and long EURAUD is not four trades. It is
one trade — long the euro — at four times the intended size, and it will win or
lose as one. The arithmetic is not subtle: every FX pair is two currencies, so
netting the legs across open signals shows the position actually being taken.

What this module does **not** do is decide anything. It cannot drop a signal,
shrink a position, or refuse a scan; that would be the software trading. It
computes the exposure, says plainly when it exceeds what the config calls a
sensible concurrent risk, and offers a subset that would fit. The human picks.

The ranking used for that subset reads the *lower bound* of a measured win rate,
never the point estimate — the same rule position sizing follows, and for the
same reason: the point estimate of a small sample is the number most likely to
be flattering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .forecast import BaseRate
from .instruments import currency_name, get_instrument
from .models import Direction, Signal


@dataclass(frozen=True)
class Leg:
    """One signal's contribution to one currency."""

    symbol: str
    direction: Direction
    currency: str
    sign: int  # +1 long the currency, -1 short it
    risk_pct: float

    @property
    def label(self) -> str:
        side = "long" if self.sign > 0 else "short"
        return f"{side} {self.currency} via {self.direction.value} {self.symbol}"


@dataclass(frozen=True)
class CurrencyExposure:
    """Net and gross exposure to one currency across every signal on the table."""

    code: str
    net_risk_pct: float
    gross_risk_pct: float
    legs: tuple[Leg, ...]

    @property
    def direction(self) -> str:
        if abs(self.net_risk_pct) < 1e-9:
            return "flat"
        return "long" if self.net_risk_pct > 0 else "short"

    @property
    def is_netted(self) -> bool:
        """True when legs partly cancel — gross risk exceeds net."""
        return self.gross_risk_pct - abs(self.net_risk_pct) > 1e-9

    def describe(self) -> str:
        if self.direction == "flat":
            return (
                f"{self.code} nets to flat across {len(self.legs)} signal(s): these "
                f"positions cancel each other, and pay the spread on both"
            )
        return (
            f"{abs(self.net_risk_pct):.2f}% of the account riding on the "
            f"{currency_name(self.code)} going {self.direction}, "
            f"across {len(self.legs)} signal(s)"
        )

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "net_risk_pct": round(self.net_risk_pct, 4),
            "gross_risk_pct": round(self.gross_risk_pct, 4),
            "direction": self.direction,
            "legs": [leg.label for leg in self.legs],
        }


@dataclass(frozen=True)
class Ranked:
    """A signal with the number used to order it, and how trustworthy that is."""

    signal: Signal
    expected_r: float
    measured: bool
    basis: str

    @property
    def symbol(self) -> str:
        return self.signal.symbol


@dataclass(frozen=True)
class ExposureReport:
    """The book that would exist if every signal on the table were taken."""

    signal_count: int
    total_risk_pct: float
    max_concurrent_pct: float
    exposures: tuple[CurrencyExposure, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    suggested: tuple[Ranked, ...] = field(default_factory=tuple)
    dropped: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def over_budget(self) -> bool:
        return self.total_risk_pct > self.max_concurrent_pct + 1e-9

    @property
    def concentrated(self) -> list[CurrencyExposure]:
        """Currencies carrying more net risk than the whole book is meant to."""
        return [e for e in self.exposures if abs(e.net_risk_pct) > self.max_concurrent_pct + 1e-9]

    def to_dict(self) -> dict:
        return {
            "signals": self.signal_count,
            "total_risk_pct": round(self.total_risk_pct, 4),
            "max_concurrent_pct": self.max_concurrent_pct,
            "over_budget": self.over_budget,
            "exposures": [e.to_dict() for e in self.exposures],
            "warnings": list(self.warnings),
            "suggested": [
                {
                    "symbol": r.symbol,
                    "direction": r.signal.direction.value,
                    "expected_r": round(r.expected_r, 3),
                    "measured": r.measured,
                    "basis": r.basis,
                }
                for r in self.suggested
            ],
            "dropped": [{"symbol": s, "reason": why} for s, why in self.dropped],
        }


def expected_r(signal: Signal, base_rate: BaseRate | None) -> Ranked:
    """What one signal is worth, in R, on the evidence available.

    With a measured base rate, the *lower bound* of the win-rate interval is
    used — not the point estimate. That is the project rule for position sizing
    and it applies with more force here, where the ranking decides which trades
    get taken at all: a pair whose 40% win rate rests on nine trades should not
    outrank one whose 30% rests on ninety.

    With no measured base rate there is no expectancy to compute, so none is
    claimed. Such signals rank below every measured one and are labelled, rather
    than being given an invented number that would sort them into the middle.
    """
    rr = signal.risk_reward
    if base_rate is not None and base_rate.is_measured:
        p = base_rate.interval.low
        return Ranked(
            signal=signal,
            expected_r=p * rr - (1 - p) * 1.0,
            measured=True,
            basis=(
                f"{base_rate.sample} past setups, win rate {base_rate.win_rate:.0%} "
                f"(low bound {p:.0%}) at {rr:.1f}R"
            ),
        )
    return Ranked(
        signal=signal,
        expected_r=float("-inf"),
        measured=False,
        basis=f"no measured base rate for {signal.symbol}; ranked on confluence only",
    )


def _legs(signal: Signal, risk_pct: float) -> list[Leg]:
    """Split one signal into its two currency legs."""
    instrument = get_instrument(signal.symbol)
    base, quote = instrument.currencies
    long = signal.direction is Direction.LONG
    return [
        Leg(signal.symbol, signal.direction, base, 1 if long else -1, risk_pct),
        Leg(signal.symbol, signal.direction, quote, -1 if long else 1, risk_pct),
    ]


def compute_exposure(signals: list[Signal], config: Config) -> list[CurrencyExposure]:
    """Net every signal's currency legs together."""
    risk_pct = config.account.risk_per_trade_pct
    buckets: dict[str, list[Leg]] = {}
    for signal in signals:
        for leg in _legs(signal, risk_pct):
            buckets.setdefault(leg.currency, []).append(leg)

    exposures = [
        CurrencyExposure(
            code=code,
            net_risk_pct=sum(leg.sign * leg.risk_pct for leg in legs),
            gross_risk_pct=sum(leg.risk_pct for leg in legs),
            legs=tuple(legs),
        )
        for code, legs in buckets.items()
    ]
    return sorted(exposures, key=lambda e: abs(e.net_risk_pct), reverse=True)


def analyse(
    signals: list[Signal],
    config: Config,
    base_rates: dict[str, BaseRate] | None = None,
) -> ExposureReport:
    """Describe the book these signals would create, and a subset that fits."""
    risk_pct = config.account.risk_per_trade_pct
    cap = config.account.max_concurrent_risk_pct
    total = risk_pct * len(signals)
    exposures = compute_exposure(signals, config)

    warnings: list[str] = []
    if total > cap + 1e-9:
        warnings.append(
            f"taking all {len(signals)} signals risks {total:.1f}% of the account at once, "
            f"above the {cap:.1f}% concurrent limit in your config"
        )
    for exposure in exposures:
        if abs(exposure.net_risk_pct) > cap + 1e-9:
            warnings.append(
                f"{exposure.code}: {exposure.describe()} — that is one bet, not "
                f"{len(exposure.legs)}, and it is above the {cap:.1f}% limit on its own"
            )
        elif len(exposure.legs) >= 3 and abs(exposure.net_risk_pct) >= risk_pct * 2:
            warnings.append(
                f"{exposure.code}: {exposure.describe()} — these move together, so expect "
                f"them to win or lose together"
            )
        elif exposure.is_netted and exposure.direction == "flat":
            warnings.append(
                f"{exposure.code}: {exposure.describe()}"
            )

    suggested, dropped = choose_subset(signals, config, base_rates)
    return ExposureReport(
        signal_count=len(signals),
        total_risk_pct=total,
        max_concurrent_pct=cap,
        exposures=tuple(exposures),
        warnings=tuple(warnings),
        suggested=tuple(suggested),
        dropped=tuple(dropped),
    )


def choose_subset(
    signals: list[Signal],
    config: Config,
    base_rates: dict[str, BaseRate] | None = None,
) -> tuple[list[Ranked], list[tuple[str, str]]]:
    """Pick the signals to actually take, best first, within the risk budget.

    Greedy on expected R, rejecting any signal that would push a currency's net
    exposure past the concurrent limit. Greedy is the right shape here: the list
    is short, the constraint is a simple cap, and a human needs to be able to
    read down the list and see why each one is in or out — which an optimiser's
    answer would not give them.

    Returns the chosen list and, for every signal left out, the reason.
    """
    risk_pct = config.account.risk_per_trade_pct
    cap = config.account.max_concurrent_risk_pct
    rates = base_rates or {}

    ranked = sorted(
        (expected_r(s, rates.get(s.symbol)) for s in signals),
        key=lambda r: (r.expected_r, r.signal.confidence, r.signal.risk_reward),
        reverse=True,
    )

    chosen: list[Ranked] = []
    dropped: list[tuple[str, str]] = []
    net: dict[str, float] = {}
    used = 0.0

    for candidate in ranked:
        if used + risk_pct > cap + 1e-9:
            dropped.append(
                (candidate.symbol, f"{cap:.1f}% concurrent risk budget already committed")
            )
            continue

        legs = _legs(candidate.signal, risk_pct)
        would = {leg.currency: net.get(leg.currency, 0.0) + leg.sign * risk_pct for leg in legs}
        breach = next(
            (code for code, value in would.items() if abs(value) > cap + 1e-9), None
        )
        if breach is not None:
            dropped.append(
                (candidate.symbol, f"would push net {breach} exposure past {cap:.1f}%")
            )
            continue

        chosen.append(candidate)
        net.update(would)
        used += risk_pct

    return chosen, dropped


# ------------------------------------------------------------------ rendering


def format_exposure(report: ExposureReport, config: Config, width: int = 74) -> str:
    """Render the exposure block for the end of a scan."""
    if report.signal_count == 0:
        return ""

    lines = [
        "-" * width,
        f"  IF YOU TOOK ALL {report.signal_count} OF THESE",
        f"    Total at risk    {report.total_risk_pct:.1f}% of the account "
        f"(your concurrent limit is {report.max_concurrent_pct:.1f}%)",
        "",
        "    Net currency exposure:",
    ]
    for exposure in report.exposures:
        if abs(exposure.net_risk_pct) < 1e-9 and len(exposure.legs) == 1:
            continue
        arrow = "long " if exposure.net_risk_pct > 0 else "short" if exposure.net_risk_pct < 0 else "flat "
        lines.append(
            f"      {exposure.code:<4} {arrow} {abs(exposure.net_risk_pct):>5.2f}%"
            f"   from {len(exposure.legs)} leg(s): "
            + ", ".join(f"{leg.direction.value} {leg.symbol}" for leg in exposure.legs[:4])
        )

    if report.warnings:
        lines.append("")
        lines.append("    Read this before taking more than one:")
        for warning in report.warnings:
            lines.append(f"      ! {warning}")

    if report.suggested:
        lines += ["", f"    If you only take {len(report.suggested)}, take these — best first:"]
        for i, ranked in enumerate(report.suggested, 1):
            value = (
                f"{ranked.expected_r:+.2f}R expected" if ranked.measured else "unmeasured"
            )
            lines.append(
                f"      {i}. {ranked.signal.direction.value:<5} {ranked.symbol:<8} "
                f"{value:<18} {ranked.basis}"
            )
    if report.dropped:
        lines.append("")
        lines.append("    Left out, and why:")
        for symbol, reason in report.dropped[:10]:
            lines.append(f"      - {symbol}: {reason}")

    lines += [
        "",
        "    This is arithmetic on your own config, not a decision. Nothing here",
        "    cancels a signal — you choose what to take.",
    ]
    return "\n".join(lines)
