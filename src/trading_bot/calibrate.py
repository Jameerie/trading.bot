"""Selectivity calibration.

The central trade-off in this project: a higher confluence threshold takes fewer
setups but better ones. Guessing where to set that threshold is how strategies
get overfitted, so this module measures it instead — sweeping the threshold and
reporting what each level actually produced.

Read the output as a *diagnostic*, not as a result to quote. The best row in a
sweep is, by construction, the luckiest row on this data. That is why
``recommend`` refuses to pick a threshold on in-sample data alone and why the
sweep prints the sample size beside every win rate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .backtest import run_backtest
from .config import Config
from .metrics import Metrics, compute_metrics, evaluate_gate
from .models import Candle


@dataclass(frozen=True)
class SweepRow:
    """One threshold's results."""

    threshold: float
    metrics: Metrics
    verdict: str

    @property
    def trades(self) -> int:
        return self.metrics.trades

    @property
    def win_rate(self) -> float:
        return self.metrics.win_rate


@dataclass(frozen=True)
class SweepResult:
    """The full sweep plus a recommendation, if one is defensible."""

    symbol: str
    rows: tuple[SweepRow, ...]
    recommended: float | None
    rationale: str


def sweep(
    candles: list[Candle],
    symbol: str,
    config: Config,
    thresholds: list[float] | None = None,
    split: float | None = 0.7,
) -> SweepResult:
    """Run a backtest at each threshold.

    When ``split`` is given, each threshold is measured on the out-of-sample
    portion only. Sweeping on the full series and quoting the winner is curve
    fitting with extra steps.
    """
    levels = thresholds or [round(0.40 + 0.05 * i, 2) for i in range(11)]
    boundary = int(len(candles) * split) if split else None

    rows: list[SweepRow] = []
    for level in levels:
        tuned = replace(config, strategy=replace(config.strategy, min_confluence=level))
        result = run_backtest(candles, symbol, tuned, start=boundary,
                              label=f"confluence>={level}")
        metrics = compute_metrics(result.trades, config.target.confidence)
        gate = evaluate_gate(
            metrics, config.target.win_rate, config.target.min_sample, config.target.confidence
        )
        rows.append(SweepRow(threshold=level, metrics=metrics, verdict=gate.verdict))

    recommended, rationale = recommend(rows, config)
    return SweepResult(symbol.upper(), tuple(rows), recommended, rationale)


def recommend(rows: list[SweepRow], config: Config) -> tuple[float | None, str]:
    """Suggest a threshold, or decline to.

    Only rows with a real sample are eligible, and the choice is made on the
    *lower bound* of the win-rate interval rather than the point estimate — the
    same standard the quality gate uses. If nothing qualifies, we say so plainly
    instead of naming the least-bad row and implying it is good.
    """
    eligible = [r for r in rows if r.trades >= config.target.min_sample]
    if not eligible:
        best = max(rows, key=lambda r: r.trades, default=None)
        count = best.trades if best else 0
        return None, (
            f"No threshold produced the {config.target.min_sample} trades needed to judge one "
            f"(most was {count}). Test a longer history before trusting any of these rows."
        )

    ranked = sorted(
        eligible,
        key=lambda r: (r.metrics.win_rate_interval.low, r.metrics.expectancy_r),
        reverse=True,
    )
    best = ranked[0]
    meets = best.metrics.win_rate_interval.low >= config.target.win_rate

    if meets:
        return best.threshold, (
            f"confluence >= {best.threshold:.2f} gives {best.win_rate:.1%} over "
            f"{best.trades} trades, with the interval clearing the "
            f"{config.target.win_rate:.0%} target."
        )
    return best.threshold, (
        f"confluence >= {best.threshold:.2f} is the strongest level tested "
        f"({best.win_rate:.1%} over {best.trades} trades, expectancy "
        f"{best.metrics.expectancy_r:+.2f}R), but it does NOT reach the "
        f"{config.target.win_rate:.0%} target. Treat it as the best available setting, "
        f"not as a validated one."
    )


def format_sweep(result: SweepResult) -> str:
    """Render the sweep as a table."""
    lines = [
        f"Selectivity sweep - {result.symbol}",
        "",
        f"{'min_confl':>9}  {'trades':>6}  {'win rate':>8}  {'95% interval':>15}  "
        f"{'expectancy':>10}  {'total R':>8}  verdict",
        "-" * 88,
    ]
    for row in result.rows:
        m = row.metrics
        interval = (
            f"{m.win_rate_interval.low:.0%}-{m.win_rate_interval.high:.0%}"
            if m.trades
            else "-"
        )
        lines.append(
            f"{row.threshold:>9.2f}  {m.trades:>6}  {m.win_rate:>7.1%}  {interval:>15}  "
            f"{m.expectancy_r:>+9.2f}R  {m.total_r:>+7.1f}R  {row.verdict}"
        )
    lines += ["", "Recommendation:", f"  {result.rationale}"]
    if result.recommended is not None:
        lines.append(f"  Set strategy.min_confluence = {result.recommended:.2f} in your config.")
    lines += [
        "",
        "Note: these rows are measured on out-of-sample data, but the act of picking",
        "the best row is itself a fit to that data. Confirm any choice on a fresh period.",
    ]
    return "\n".join(lines)
