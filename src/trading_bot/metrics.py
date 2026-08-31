"""Performance measurement.

The headline number this project cares about is win rate, which makes it the
number most in need of honest handling. Three things are done here that a naive
report would skip:

1. **Every win rate comes with a Wilson score interval.** Six wins from seven
   trades is 85.7%, and it is also entirely consistent with a coin-flip strategy.
   The interval says so.
2. **The quality gate tests the interval's lower bound, not the point estimate.**
   A strategy "meets the target" only when the evidence rules out its being
   worse — not when a small lucky sample happens to land above the line.
3. **Expired trades count.** A trade closed by the time limit is a real outcome
   with a real P&L. Excluding it because it is neither a clean win nor a clean
   loss would flatter every metric here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Outcome, Trade


@dataclass(frozen=True)
class Interval:
    """A confidence interval on a proportion."""

    low: float
    high: float
    confidence: float

    def __str__(self) -> str:
        return f"{self.low:.1%}-{self.high:.1%} @ {self.confidence:.0%}"


def wilson_interval(successes: int, total: int, confidence: float = 0.95) -> Interval:
    """Wilson score interval for a binomial proportion.

    Chosen over the textbook normal approximation because it stays sane for the
    small samples and extreme proportions this tool routinely produces — a
    strategy with 8 trades and 7 wins breaks the normal approximation entirely.
    """
    if total <= 0:
        return Interval(0.0, 1.0, confidence)
    if not 0.5 <= confidence < 1:
        raise ValueError(f"confidence must be in [0.5, 1), got {confidence}")

    z = _z_for(confidence)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return Interval(max(0.0, centre - margin), min(1.0, centre + margin), confidence)


def _z_for(confidence: float) -> float:
    """Two-sided normal critical value, via the inverse error function."""
    # math.erfinv does not exist; invert erf by bisection. Cheap and exact enough,
    # and it keeps this module dependency-free.
    target = confidence
    lo, hi = 0.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if math.erf(mid / math.sqrt(2)) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def mean_interval(values: list[float], confidence: float = 0.95) -> Interval:
    """Confidence interval on the *mean* of a sample, not on a proportion.

    Win rate has Wilson; expectancy needs its own bound, because ranking pairs by
    a point-estimate expectancy is how the luckiest pair in a list of thirty gets
    mistaken for the best one. This is the ordinary standard-error interval on
    the mean, which is sound here for the reason it usually is not in finance:
    we are averaging R-multiples that are bounded below at -1, so the tail that
    breaks the normal approximation is the *upside*, and an overstated upper
    bound cannot flatter a decision made on the lower one.

    The interval is returned in the same units as the values (R), so it is not
    clamped to [0, 1] the way a proportion is.
    """
    n = len(values)
    if n == 0:
        return Interval(0.0, 0.0, confidence)
    mean = sum(values) / n
    if n == 1:
        # One observation says nothing about the precision of a mean, so the
        # honest interval is unbounded. Returning (mean, mean) instead — a
        # zero-width interval — would let a single winning trade pass any
        # lower-bound test put to it, which is the exact failure this whole
        # module is built to prevent.
        return Interval(float("-inf"), float("inf"), confidence)
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    stderr = math.sqrt(variance / n)
    margin = _z_for(confidence) * stderr
    return Interval(mean - margin, mean + margin, confidence)


def family_confidence(confidence: float, comparisons: int) -> float:
    """Per-test confidence needed for ``comparisons`` intervals to hold together.

    Test one pair at 95% and a one-in-twenty chance of a spurious result is
    acceptable. Test sixty pairs at 95% and roughly three of them will look
    significant on noise alone — then the best-looking pair gets picked, which is
    how a universe scan manufactures an edge that is not there.

    Šidák's correction: for all ``n`` intervals to cover simultaneously at level
    ``C``, each must be built at level ``C ** (1/n)``. At 95% across 30 pairs
    that is 99.83% per pair, and the intervals widen accordingly. Nothing else
    changes — the same win rates are reported, they are simply held to the
    standard that looking at thirty of them demands.
    """
    if comparisons <= 1:
        return confidence
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    return confidence ** (1.0 / comparisons)


@dataclass(frozen=True)
class Metrics:
    """Everything worth knowing about a set of trades."""

    trades: int
    wins: int
    losses: int
    breakeven: int
    expired: int
    win_rate: float
    win_rate_interval: Interval
    expectancy_r: float
    total_r: float
    profit_factor: float
    average_win_r: float
    average_loss_r: float
    max_drawdown_r: float
    max_win_streak: int
    max_loss_streak: int
    average_bars_held: float
    average_rr_planned: float
    average_mae_r: float
    average_mfe_r: float

    @property
    def is_empty(self) -> bool:
        return self.trades == 0


def compute_metrics(trades: list[Trade], confidence: float = 0.95) -> Metrics:
    """Summarise a list of trades.

    A trade counts as a win if it ended positive in R terms. That definition
    deliberately includes an expired trade that closed in profit and excludes one
    that closed in loss, rather than quietly dropping both.
    """
    if not trades:
        return Metrics(
            0, 0, 0, 0, 0, 0.0, Interval(0.0, 1.0, confidence), 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0, 0.0,
        )

    r_values = [t.r_multiple for t in trades]
    wins = [t for t in trades if t.r_multiple > 1e-9]
    losses = [t for t in trades if t.r_multiple < -1e-9]
    breakeven = [t for t in trades if abs(t.r_multiple) <= 1e-9]
    expired = [t for t in trades if t.outcome is Outcome.EXPIRED]

    total_r = sum(r_values)
    gross_win = sum(t.r_multiple for t in wins)
    gross_loss = abs(sum(t.r_multiple for t in losses))

    win_rate = len(wins) / len(trades)

    return Metrics(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        breakeven=len(breakeven),
        expired=len(expired),
        win_rate=win_rate,
        win_rate_interval=wilson_interval(len(wins), len(trades), confidence),
        expectancy_r=total_r / len(trades),
        total_r=total_r,
        # An infinite profit factor is not informative; report it as the gross
        # win instead so the caller can print a finite number.
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float(gross_win),
        average_win_r=(gross_win / len(wins)) if wins else 0.0,
        average_loss_r=(-gross_loss / len(losses)) if losses else 0.0,
        max_drawdown_r=max_drawdown(r_values),
        max_win_streak=longest_streak(trades, win=True),
        max_loss_streak=longest_streak(trades, win=False),
        average_bars_held=sum(t.bars_held for t in trades) / len(trades),
        average_rr_planned=sum(t.signal.risk_reward for t in trades) / len(trades),
        average_mae_r=sum(t.mae_r for t in trades) / len(trades),
        average_mfe_r=sum(t.mfe_r for t in trades) / len(trades),
    )


def max_drawdown(r_values: list[float]) -> float:
    """Largest peak-to-trough decline of the cumulative R curve.

    Reported as a positive number of R. This is the number that decides whether a
    strategy is survivable in practice: a 4R expectancy is no use if getting
    there costs a 20R trough the account cannot sit through.
    """
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for r in r_values:
        equity += r
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def longest_streak(trades: list[Trade], win: bool) -> int:
    """Longest consecutive run of winners or losers."""
    best = current = 0
    for trade in trades:
        is_win = trade.r_multiple > 1e-9
        if is_win == win:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def equity_curve(trades: list[Trade], starting: float = 0.0) -> list[float]:
    """Cumulative R after each trade."""
    curve, total = [], starting
    for trade in trades:
        total += trade.r_multiple
        curve.append(round(total, 4))
    return curve


@dataclass(frozen=True)
class QualityGate:
    """Whether the measured performance clears the target, and why or why not."""

    target_win_rate: float
    min_sample: int
    passed: bool
    verdict: str
    detail: str


def evaluate_gate(
    metrics: Metrics, target_win_rate: float, min_sample: int, confidence: float = 0.95
) -> QualityGate:
    """Judge a result against the target win rate.

    The test is on the *lower bound* of the confidence interval. This is strict
    on purpose: the alternative — passing whenever the point estimate clears the
    line — would hand out a "meets target" badge to any strategy that got lucky
    over ten trades, which is exactly the failure this project must not have.
    """
    if metrics.trades < min_sample:
        return QualityGate(
            target_win_rate=target_win_rate,
            min_sample=min_sample,
            passed=False,
            verdict="INSUFFICIENT DATA",
            detail=(
                f"{metrics.trades} trade(s) is below the {min_sample}-trade minimum. "
                f"No win rate is claimed from this sample. Test a longer history or "
                f"lower strategy.min_confluence to take more setups."
            ),
        )

    interval = metrics.win_rate_interval
    if interval.low >= target_win_rate:
        return QualityGate(
            target_win_rate=target_win_rate,
            min_sample=min_sample,
            passed=True,
            verdict="MEETS TARGET",
            detail=(
                f"win rate {metrics.win_rate:.1%} over {metrics.trades} trades; even the "
                f"low end of the {interval.confidence:.0%} interval ({interval.low:.1%}) "
                f"clears the {target_win_rate:.0%} target."
            ),
        )
    if metrics.win_rate >= target_win_rate:
        return QualityGate(
            target_win_rate=target_win_rate,
            min_sample=min_sample,
            passed=False,
            verdict="UNPROVEN",
            detail=(
                f"win rate {metrics.win_rate:.1%} is above the {target_win_rate:.0%} target, "
                f"but the {interval.confidence:.0%} interval reaches down to {interval.low:.1%}. "
                f"The sample is too small to rule out a much worse strategy. Needs more trades."
            ),
        )
    return QualityGate(
        target_win_rate=target_win_rate,
        min_sample=min_sample,
        passed=False,
        verdict="BELOW TARGET",
        detail=(
            f"win rate {metrics.win_rate:.1%} over {metrics.trades} trades is below the "
            f"{target_win_rate:.0%} target (interval {interval.low:.1%}-{interval.high:.1%}). "
            f"Expectancy is {metrics.expectancy_r:+.2f}R per trade, so the strategy is "
            f"{'still profitable' if metrics.expectancy_r > 0 else 'losing money'} at this win rate."
        ),
    )


# ----------------------------------------------------------------------- edge


def random_baseline(risk_reward: float) -> float:
    """The win rate luck alone produces at this reward-to-risk ratio.

    A driftless random walk placed between two barriers touches each one with
    probability proportional to the *other* one's distance, so a target set ``R``
    times further away than the stop is reached first only ``1 / (1 + R)`` of the
    time. At our 1:4 floor that is 20%. At 1:1.5 it is 40%.

    This is the number a strategy must beat to have demonstrated anything at all,
    and it is why a bare win rate cannot be compared across systems: 45% at 1.5:1
    is a *smaller* edge than 25% at 4:1, though the first looks nearly twice as
    good. Quoting a win rate without its ratio is the most common way a track
    record misleads, including by accident.
    """
    if risk_reward <= 0:
        raise ValueError(f"risk_reward must be positive, got {risk_reward}")
    return 1.0 / (1.0 + risk_reward)


@dataclass(frozen=True)
class Edge:
    """How far a measured win rate sits above what chance would have given."""

    risk_reward: float
    baseline: float
    win_rate: float
    edge: float
    lower_bound_edge: float
    proven: bool
    verdict: str
    detail: str


def effective_ratio(metrics: Metrics) -> float:
    """The reward-to-risk ratio to hold a result to.

    A planned 6.6:1 that in practice pays 4.4:1 — because trades keep closing on
    the time limit instead of at the target — must not be scored against a 6.6:1
    baseline. The planned ratio sets a *lower* bar (chance clears a distant target
    less often), so using it where trades never reach that target inflates the
    apparent edge. That is the exact direction of error this project exists to
    avoid, so the harder of the two bars is always used.
    """
    planned = metrics.average_rr_planned
    if metrics.wins == 0 or metrics.losses == 0 or metrics.average_loss_r == 0:
        return planned
    realised = metrics.average_win_r / abs(metrics.average_loss_r)
    return min(planned, realised) if realised > 0 else planned


def measure_edge(metrics: Metrics, min_sample: int = 30) -> Edge:
    """Compare a result against its own random-walk baseline.

    Like :func:`evaluate_gate`, this refuses to reach a verdict on a small sample
    and decides on the *lower bound* of the win-rate interval rather than the
    point estimate. A strategy has shown an edge only when the evidence rules out
    chance having produced the result.

    The ratio used is :func:`effective_ratio`, not the planned one, so a strategy
    is never credited for a target it did not actually reach.

    One approximation remains, and it errs in the safe direction: the baseline
    ignores spread and commission, which push a real random walk *below*
    ``1 / (1 + R)``. The true bar is therefore slightly lower than the one applied
    here, so this measure understates the edge rather than flattering it.
    """
    if metrics.is_empty:
        return Edge(0.0, 0.0, 0.0, 0.0, 0.0, False, "NO DATA", "No trades to measure.")

    rr = effective_ratio(metrics)
    if rr <= 0:
        return Edge(
            rr, 0.0, metrics.win_rate, 0.0, 0.0, False, "NO DATA",
            "Reward-to-risk is not positive; no baseline can be formed.",
        )

    baseline = random_baseline(rr)
    interval = metrics.win_rate_interval
    edge = metrics.win_rate - baseline
    lower = interval.low - baseline
    against = (
        f"{metrics.win_rate:.1%} against a {baseline:.1%} chance baseline at {rr:.1f}:1"
    )

    if metrics.trades < min_sample:
        return Edge(
            rr, baseline, metrics.win_rate, edge, lower, False, "INSUFFICIENT DATA",
            f"{against} — {edge * 100:+.1f} points, but {metrics.trades} trade(s) is "
            f"below the {min_sample}-trade minimum. No edge is claimed from this sample.",
        )

    expired_share = metrics.expired / metrics.trades
    caveat = (
        f" {metrics.expired} of {metrics.trades} trades closed on the time limit rather "
        f"than at a barrier, which weakens this comparison."
        if expired_share > 0.2
        else ""
    )

    proven = lower > 0
    if proven:
        verdict = "EDGE CONFIRMED"
        detail = (
            f"{against} — {edge * 100:+.1f} points. Even the low end of the interval "
            f"({interval.low:.1%}) clears chance, so the result is not luck.{caveat}"
        )
    elif edge > 0:
        verdict = "UNPROVEN"
        detail = (
            f"{against} — {edge * 100:+.1f} points, but the interval reaches down to "
            f"{interval.low:.1%}, below chance. A coin flip could have produced this. "
            f"Needs more trades.{caveat}"
        )
    else:
        verdict = "NO EDGE"
        detail = (
            f"{against} — {edge * 100:+.1f} points. The strategy is not beating a "
            f"random walk at this ratio.{caveat}"
        )

    return Edge(
        risk_reward=rr,
        baseline=baseline,
        win_rate=metrics.win_rate,
        edge=edge,
        lower_bound_edge=lower,
        proven=proven,
        verdict=verdict,
        detail=detail,
    )
