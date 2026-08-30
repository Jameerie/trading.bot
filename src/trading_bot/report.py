"""Rendering of backtest results.

Every report leads with the sample size and the out-of-sample flag, because a
win rate without those two numbers beside it is not information.
"""

from __future__ import annotations

from .backtest import BacktestResult
from .config import Config
from .metrics import Metrics, QualityGate, compute_metrics, equity_curve, evaluate_gate


def _profit_factor(metrics: Metrics) -> str:
    if metrics.losses == 0:
        return "n/a (no losing trades)"
    return f"{metrics.profit_factor:.2f}"


def format_result(result: BacktestResult, config: Config, width: int = 78) -> str:
    """Render one backtest as a text block."""
    metrics = compute_metrics(result.trades, config.target.confidence)
    gate = evaluate_gate(
        metrics, config.target.win_rate, config.target.min_sample, config.target.confidence
    )
    bar = "=" * width

    lines = [
        bar,
        f"  {result.symbol}  -  {result.label}",
        f"  {result.first_bar[:16].replace('T', ' ')} to {result.last_bar[:16].replace('T', ' ')} UTC"
        f"  ({result.bars_tested} bars)",
        bar,
    ]

    if metrics.is_empty:
        lines += [
            "",
            "  No trades taken.",
            "",
            f"  The strategy scanned {result.bars_tested} bars and found nothing that met",
            f"  both the {config.strategy.min_confluence:.0%} confluence threshold and the "
            f"{config.risk.min_risk_reward:.0f}:1 reward floor.",
            "",
            "  That is a valid outcome, not a bug. To take more setups, lower",
            "  strategy.min_confluence — and re-read the win rate afterwards.",
            bar,
        ]
        return "\n".join(lines)

    lines += [
        "",
        "  RESULT",
        f"    Trades            {metrics.trades}",
        f"    Win rate          {metrics.win_rate:.1%}   "
        f"({metrics.wins}W / {metrics.losses}L / {metrics.breakeven}BE)",
        f"    {int(config.target.confidence * 100)}% interval     "
        f"{metrics.win_rate_interval.low:.1%} to {metrics.win_rate_interval.high:.1%}",
        f"    Expectancy        {metrics.expectancy_r:+.2f}R per trade",
        f"    Total             {metrics.total_r:+.1f}R",
        f"    Profit factor     {_profit_factor(metrics)}",
        f"    Max drawdown      {metrics.max_drawdown_r:.1f}R",
        "",
        "  DETAIL",
        f"    Average win       {metrics.average_win_r:+.2f}R",
        f"    Average loss      {metrics.average_loss_r:+.2f}R",
        f"    Planned R:R       {metrics.average_rr_planned:.1f} (average)",
        f"    Longest streaks   {metrics.max_win_streak} wins / {metrics.max_loss_streak} losses",
        f"    Bars held         {metrics.average_bars_held:.0f} (average)",
        f"    Excursion         MAE {metrics.average_mae_r:.2f}R / MFE {metrics.average_mfe_r:+.2f}R",
        f"    Expired           {metrics.expired} (hit the {config.backtest.max_bars_in_trade}-bar limit)",
        "",
        "  SIGNAL FLOW",
        f"    Signals taken     {result.signals_generated}",
        f"    Bars scanned      {result.bars_tested}",
        f"    Selectivity       {result.signals_generated / max(result.bars_tested, 1):.2%} of bars",
        "",
        "-" * width,
        f"  TARGET: {config.target.win_rate:.0%} win rate",
        f"  {gate.verdict}",
    ]
    for chunk in _wrap(gate.detail, width - 6):
        lines.append(f"    {chunk}")

    lines += ["", _cash_note(metrics, config), bar]
    return "\n".join(lines)


def _cash_note(metrics: Metrics, config: Config) -> str:
    """Translate R into account currency, since that is what a user feels."""
    risk_per_trade = config.account.balance * (config.account.risk_per_trade_pct / 100.0)
    total = metrics.total_r * risk_per_trade
    drawdown = metrics.max_drawdown_r * risk_per_trade
    return (
        f"  At {config.account.risk_per_trade_pct:.1f}% risk on "
        f"{config.account.balance:,.0f} {config.account.currency}: "
        f"{total:+,.0f} total, {drawdown:,.0f} worst drawdown.\n"
        f"  Past simulated results do not predict future trades."
    )


def _wrap(text: str, width: int) -> list[str]:
    """Minimal word wrap. Avoids a textwrap import for one call site."""
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def format_comparison(
    in_sample: BacktestResult, out_sample: BacktestResult, config: Config
) -> str:
    """Render in-sample against out-of-sample, and call out the gap."""
    parts = [format_result(in_sample, config), "", format_result(out_sample, config), ""]

    a = compute_metrics(in_sample.trades, config.target.confidence)
    b = compute_metrics(out_sample.trades, config.target.confidence)
    if a.is_empty or b.is_empty:
        parts.append(
            "  Not enough trades in one of the halves to compare in-sample with out-of-sample."
        )
        return "\n".join(parts)

    gap = a.win_rate - b.win_rate
    parts += [
        "=" * 78,
        "  IN-SAMPLE vs OUT-OF-SAMPLE",
        "=" * 78,
        f"    In-sample      {a.win_rate:>6.1%} over {a.trades:>3} trades   "
        f"{a.expectancy_r:+.2f}R",
        f"    Out-of-sample  {b.win_rate:>6.1%} over {b.trades:>3} trades   "
        f"{b.expectancy_r:+.2f}R",
        f"    Gap            {gap:+.1%}",
        "",
    ]
    if gap > 0.15:
        parts.append(
            "    The strategy performs materially worse on data it was not tuned on."
        )
        parts.append("    Quote the out-of-sample number, not the in-sample one.")
    elif gap < -0.15:
        parts.append(
            "    Out-of-sample beat in-sample, which usually means the two periods"
        )
        parts.append("    had different market conditions rather than that the edge grew.")
    else:
        parts.append("    The two halves agree, which is what a robust setting looks like.")
    parts.append("=" * 78)
    return "\n".join(parts)


def format_trades(result: BacktestResult, limit: int = 25) -> str:
    """Tabulate individual trades, so a result can be audited rather than trusted."""
    if not result.trades:
        return "No trades to list."
    lines = [
        f"{'entry (UTC)':<17} {'dir':<5} {'entry':>9} {'exit':>9} "
        f"{'outcome':<9} {'R':>7} {'bars':>5} {'grade':>5}",
        "-" * 76,
    ]
    for trade in result.trades[:limit]:
        lines.append(
            f"{trade.entry_time.strftime('%Y-%m-%d %H:%M'):<17} "
            f"{trade.signal.direction.value:<5} "
            f"{trade.signal.entry:>9.5f} "
            f"{(trade.exit_price or 0):>9.5f} "
            f"{trade.outcome.value:<9} "
            f"{trade.r_multiple:>+7.2f} "
            f"{trade.bars_held:>5} "
            f"{trade.signal.grade:>5}"
        )
    if len(result.trades) > limit:
        lines.append(f"... {len(result.trades) - limit} more")
    curve = equity_curve(result.trades)
    lines += ["", f"Cumulative R: {curve[-1]:+.2f} (peak {max(curve):+.2f}, trough {min(curve):+.2f})"]
    return "\n".join(lines)
