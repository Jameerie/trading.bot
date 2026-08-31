"""Position sizing analysis: how much to risk, and what it costs to be wrong.

A 1:4 system is profitable above a 20% win rate, so the interesting question is
not "does this make money" but "how hard can I push before pushing makes it
worse". This module answers that from a *measured* win rate rather than a hoped
one.

Three results drive the recommendation:

1. **Kelly is a ceiling, not a target.** Past the Kelly fraction, median growth
   falls while drawdown keeps climbing. Betting harder than optimal is worse
   than betting less than optimal, in both directions at once.
2. **Full Kelly is unusable in practice.** At a 30% win rate it implies a median
   drawdown near 74%. Nobody sits through that, and an account that halves needs
   a double just to get back.
3. **Overestimating the edge is not a small error.** Sizing for 40% when the
   truth is 25% does not cost a slice of the return, it inverts the outcome. So
   sizing is done off the *lower bound* of the win-rate interval, never the
   point estimate — the same standard ``metrics.evaluate_gate`` applies.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .metrics import Metrics, wilson_interval

# Fraction of Kelly considered safe for discretionary trading. Half-Kelly is the
# common textbook compromise; a quarter is what survives a wrong estimate.
CONSERVATIVE_KELLY_FRACTION = 0.25
DEFAULT_TRIALS = 5000
DEFAULT_HORIZON = 60


def expectancy_r(win_rate: float, reward: float) -> float:
    """Expected R per trade at a given win rate and reward multiple."""
    return win_rate * reward - (1.0 - win_rate)


def breakeven_win_rate(reward: float) -> float:
    """The win rate at which a given reward multiple stops losing money."""
    if reward <= 0:
        raise ValueError("reward multiple must be positive")
    return 1.0 / (1.0 + reward)


def kelly_fraction(win_rate: float, reward: float) -> float:
    """Growth-optimal fraction of balance to risk per trade.

    Clamped at zero: a negative Kelly means the edge is negative, and the correct
    size is then no position at all rather than a short one.
    """
    if reward <= 0:
        raise ValueError("reward multiple must be positive")
    loss_rate = 1.0 - win_rate
    return max(0.0, (win_rate * reward - loss_rate) / reward)


def log_growth_rate(win_rate: float, risk_fraction: float, reward: float) -> float:
    """Expected log growth per trade. Negative means the account shrinks.

    Uses logs because returns compound: the arithmetic mean of a compounding
    series overstates what a single account actually experiences.
    """
    if not 0 < risk_fraction < 1:
        return float("-inf")
    return win_rate * math.log(1 + reward * risk_fraction) + (1 - win_rate) * math.log(
        1 - risk_fraction
    )


@dataclass(frozen=True)
class SizingRow:
    """Simulated outcome of one risk-per-trade setting."""

    risk_fraction: float
    median_multiple: float
    p05_multiple: float
    p95_multiple: float
    median_drawdown: float
    p95_drawdown: float
    prob_lose_half: float
    label: str = ""

    @property
    def median_return_pct(self) -> float:
        return (self.median_multiple - 1.0) * 100.0


def simulate_sizing(
    win_rate: float,
    risk_fraction: float,
    reward: float,
    trades: int = DEFAULT_HORIZON,
    trials: int = DEFAULT_TRIALS,
    seed: int = 20240101,
) -> SizingRow:
    """Monte Carlo one risk setting over a trading horizon.

    Seeded so the same inputs always give the same answer — a sizing
    recommendation that wobbles between runs is not a recommendation.
    """
    if not 0 < risk_fraction < 1:
        raise ValueError(f"risk_fraction must be in (0, 1), got {risk_fraction}")
    if trades < 1 or trials < 1:
        raise ValueError("trades and trials must both be >= 1")

    rng = random.Random(seed)
    win_multiple = 1.0 + reward * risk_fraction
    loss_multiple = 1.0 - risk_fraction

    finals: list[float] = []
    drawdowns: list[float] = []
    for _ in range(trials):
        equity = peak = 1.0
        worst = 0.0
        for _ in range(trades):
            equity *= win_multiple if rng.random() < win_rate else loss_multiple
            peak = max(peak, equity)
            worst = max(worst, 1.0 - equity / peak)
        finals.append(equity)
        drawdowns.append(worst)

    finals.sort()
    drawdowns.sort()
    return SizingRow(
        risk_fraction=risk_fraction,
        median_multiple=finals[trials // 2],
        p05_multiple=finals[int(0.05 * trials)],
        p95_multiple=finals[int(0.95 * trials)],
        median_drawdown=drawdowns[trials // 2],
        p95_drawdown=drawdowns[int(0.95 * trials)],
        prob_lose_half=sum(1 for value in finals if value < 0.5) / trials,
    )


@dataclass(frozen=True)
class SizingReport:
    """A full sizing analysis for one measured (or assumed) win rate."""

    win_rate: float
    win_rate_source: str
    reward: float
    trades_per_period: int
    breakeven: float
    expectancy: float
    kelly: float
    recommended_risk: float
    rows: tuple[SizingRow, ...]
    caution: str

    @property
    def is_profitable(self) -> bool:
        return self.expectancy > 0


def analyse(
    win_rate: float,
    reward: float = 4.0,
    trades: int = DEFAULT_HORIZON,
    trials: int = DEFAULT_TRIALS,
    source: str = "assumed",
    max_risk: float = 0.05,
) -> SizingReport:
    """Build the sizing report for a win rate.

    ``max_risk`` caps the recommendation regardless of what Kelly says. Even a
    correct Kelly of 12% is not a sane discretionary size, and the cap is what
    stops this function handing someone a number that will end their account on
    a normal losing streak.
    """
    expectancy = expectancy_r(win_rate, reward)
    kelly = kelly_fraction(win_rate, reward)
    breakeven = breakeven_win_rate(reward)

    if expectancy <= 0:
        return SizingReport(
            win_rate=win_rate,
            win_rate_source=source,
            reward=reward,
            trades_per_period=trades,
            breakeven=breakeven,
            expectancy=expectancy,
            kelly=0.0,
            recommended_risk=0.0,
            rows=(),
            caution=(
                f"A {win_rate:.1%} win rate at {reward:.0f}:1 loses money — breakeven is "
                f"{breakeven:.1%}. No position size makes a negative edge profitable; "
                f"sizing up only loses faster. Raise selectivity or stop trading this setup."
            ),
        )

    recommended = min(kelly * CONSERVATIVE_KELLY_FRACTION, max_risk)
    # Below a tenth of a percent the position rounds to nothing at retail lot
    # sizes, so there is no point recommending it. Rounded once here so the
    # label lookup below compares against the same value the row was built from.
    recommended = round(max(recommended, 0.001), 4)

    half_kelly = round(kelly / 2, 4)
    full_kelly = round(kelly, 4)
    candidates = sorted({0.005, 0.01, 0.02, 0.03, 0.05, recommended, half_kelly, full_kelly})
    rows = []
    for fraction in candidates:
        if not 0 < fraction < 1:
            continue
        row = simulate_sizing(win_rate, fraction, reward, trades, trials)
        # Recommended wins the label when it coincides with a Kelly marker: it is
        # the number the user is meant to act on.
        label = ""
        if fraction == recommended:
            label = "recommended"
        elif fraction == full_kelly:
            label = "full Kelly - unusable in practice"
        elif fraction == half_kelly:
            label = "half Kelly"
        rows.append(
            SizingRow(
                risk_fraction=row.risk_fraction,
                median_multiple=row.median_multiple,
                p05_multiple=row.p05_multiple,
                p95_multiple=row.p95_multiple,
                median_drawdown=row.median_drawdown,
                p95_drawdown=row.p95_drawdown,
                prob_lose_half=row.prob_lose_half,
                label=label,
            )
        )

    return SizingReport(
        win_rate=win_rate,
        win_rate_source=source,
        reward=reward,
        trades_per_period=trades,
        breakeven=breakeven,
        expectancy=expectancy,
        kelly=kelly,
        recommended_risk=recommended,
        rows=tuple(rows),
        caution=(
            f"Sized from the {source} win rate. Kelly is {kelly:.1%}, but the recommendation is "
            f"{recommended:.1%} — a quarter of Kelly, capped at {max_risk:.0%}. Overestimating "
            f"the edge does not cost a slice of the return, it inverts the outcome."
        ),
    )


def analyse_from_metrics(
    metrics: Metrics,
    reward: float = 4.0,
    trades: int = DEFAULT_HORIZON,
    trials: int = DEFAULT_TRIALS,
    confidence: float = 0.95,
    use_lower_bound: bool = True,
) -> SizingReport:
    """Size from a backtest result, defaulting to the pessimistic end.

    ``use_lower_bound`` is the whole point. The measured win rate is a sample,
    and sizing off the sample mean bets that the sample was not lucky. Sizing off
    the lower bound bets only on what the evidence actually supports.
    """
    if metrics.is_empty:
        raise ValueError("cannot size a position from zero trades")

    interval = wilson_interval(metrics.wins, metrics.trades, confidence)
    if use_lower_bound:
        rate = interval.low
        source = (
            f"measured, lower bound of the {confidence:.0%} interval "
            f"({metrics.win_rate:.1%} observed over {metrics.trades} trades, "
            f"interval {interval.low:.1%}-{interval.high:.1%})"
        )
    else:
        rate = metrics.win_rate
        source = f"measured point estimate over {metrics.trades} trades"

    return analyse(rate, reward, trades, trials, source)


def misestimation_grid(
    assumed_rates: list[float],
    true_rates: list[float],
    reward: float = 4.0,
    trades: int = DEFAULT_HORIZON,
    trials: int = 2000,
) -> list[tuple[float, float, list[float]]]:
    """What full-Kelly sizing on an assumed rate returns under other true rates.

    Returns ``(assumed_rate, risk_used, [median multiple per true rate])``. The
    asymmetry it exposes is the argument for the lower-bound default above.
    """
    grid = []
    for assumed in assumed_rates:
        fraction = kelly_fraction(assumed, reward)
        if fraction <= 0:
            grid.append((assumed, 0.0, [1.0] * len(true_rates)))
            continue
        medians = [
            simulate_sizing(true_rate, fraction, reward, trades, trials).median_multiple
            for true_rate in true_rates
        ]
        grid.append((assumed, fraction, medians))
    return grid


def format_report(report: SizingReport, balance: float = 10_000.0, currency: str = "USD") -> str:
    """Render the sizing analysis as text."""
    width = 82
    lines = [
        "=" * width,
        f"  POSITION SIZING  -  {report.reward:.0f}:1 reward, {report.trades_per_period} trades per period",
        "=" * width,
        "",
        f"  Win rate used     {report.win_rate:.1%}",
        f"  Source            {report.win_rate_source}",
        f"  Breakeven         {report.breakeven:.1%}  (below this, {report.reward:.0f}:1 loses money)",
        f"  Expectancy        {report.expectancy:+.2f}R per trade",
    ]

    if not report.is_profitable:
        lines += ["", "  " + report.caution, "=" * width]
        return "\n".join(lines)

    lines += [
        f"  Full Kelly        {report.kelly:.1%}",
        "",
        f"  RECOMMENDED RISK  {report.recommended_risk:.2%} "
        f"= {balance * report.recommended_risk:,.0f} {currency} per trade on {balance:,.0f}",
        "",
        "-" * width,
        f"{'risk':>6} {'median':>9} {'5th pct':>9} {'95th pct':>9} {'med DD':>8} "
        f"{'95th DD':>8} {'P(-50%)':>9}",
        "-" * width,
    ]
    for row in report.rows:
        marker = f"  <- {row.label}" if row.label else ""
        lines.append(
            f"{row.risk_fraction:>5.1%} {row.median_multiple:>8.2f}x {row.p05_multiple:>8.2f}x "
            f"{row.p95_multiple:>8.2f}x {row.median_drawdown:>7.0%} {row.p95_drawdown:>7.0%} "
            f"{row.prob_lose_half:>8.1%}{marker}"
        )

    lines += [
        "",
        "  Past the Kelly fraction the median return falls while drawdown keeps rising:",
        "  betting harder than optimal is worse than betting softer, on both counts.",
        "",
        "  " + report.caution,
        "=" * width,
    ]
    return "\n".join(lines)
