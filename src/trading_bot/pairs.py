"""Win rate by pair, and which pairs are worth your money.

Scanning sixty instruments instead of three is straightforwardly good: a setup
you never looked at is a setup you never had. Judging sixty instruments is where
it gets dangerous, and this module exists for the dangerous half.

Three things happen here that a plain per-pair table would get wrong:

1. **Every pair is accounted for, including the ones with no answer.** A pair
   with no data and a pair with a 60% win rate over five trades are both
   reported, both labelled, and neither is quietly dropped. A table that lists
   only the pairs that produced trades is a table that has already filtered on
   the outcome.

2. **The intervals are widened for the fact that you looked at sixty pairs.**
   At 95% confidence, three pairs in sixty clear the bar on noise alone — and
   the eye goes straight to them. Šidák's correction (``metrics.family_confidence``)
   raises the per-pair standard so that *all* the intervals hold together at
   95%. Pairs are ranked and recommended on the corrected bound, never the raw
   one.

3. **Currencies are scored as well as pairs.** EURUSD, EURGBP and EURJPY are not
   three independent readings; they share a leg. Aggregating by currency shows
   whether it is the euro that has been paying or one particular pair that got
   lucky, which a pair-by-pair table cannot distinguish.

The output ranks by the lower bound of expectancy, not by win rate. Win rate
without its ratio ranks a 45%-at-1.5:1 pair above a 30%-at-6:1 pair, and the
second one makes twice the money.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .backtest import BacktestResult, run_backtest, split_backtest
from .config import Config
from .instruments import get_instrument
from .metrics import (
    Edge,
    Interval,
    Metrics,
    compute_metrics,
    effective_ratio,
    family_confidence,
    mean_interval,
    measure_edge,
    random_baseline,
)
from .models import Candle, Trade

def _finite(value: float) -> float | None:
    """JSON has no infinity. An unbounded interval serialises as null.

    ``mean_interval`` returns an unbounded interval for a single observation on
    purpose, and ``json.dumps`` would happily emit a bare ``-Infinity`` that no
    standards-compliant parser — the browser's included — will read back.
    """
    return value if math.isfinite(value) else None


# Status values a pair can end up with. Every symbol asked about lands on one.
STATUS_MEASURED = "measured"
STATUS_THIN = "thin sample"
STATUS_NO_TRADES = "no setups"
STATUS_NO_DATA = "no data"


@dataclass(frozen=True)
class PairReport:
    """Everything measured about one instrument, and how far to trust it."""

    symbol: str
    group: str
    status: str
    bars_tested: int
    trades: list[Trade]
    metrics: Metrics
    edge: Edge
    family_interval: Interval
    expectancy_interval: Interval
    family_conf: float
    note: str = ""
    window: str = ""

    @property
    def has_trades(self) -> bool:
        return self.metrics.trades > 0

    @property
    def baseline(self) -> float:
        """The win rate chance alone would give at the ratio these trades reached."""
        if not self.has_trades:
            return 0.0
        return random_baseline(max(effective_ratio(self.metrics), 1e-9))

    @property
    def survives_correction(self) -> bool:
        """Whether this pair still beats chance once the whole scan is accounted for.

        This is the only "yes" in the module that means anything. The raw 95%
        interval clearing chance means the pair looked good; this means it still
        looks good after allowing for how many pairs were inspected to find it.

        The sample gate is load-bearing, not belt-and-braces. A pair with one
        winning trade and no losers has no *realised* ratio, so ``effective_ratio``
        falls back to the planned one — and a distant planned target sets a
        chance baseline near 11%, which a single win clears. The interval is not
        lying; the question is. Below the configured minimum sample this pair has
        not been measured, and an unmeasured pair does not get a verdict.
        """
        return (
            self.status == STATUS_MEASURED
            and self.has_trades
            and self.family_interval.low > self.baseline
        )

    @property
    def profitable_lower_bound(self) -> bool:
        """Whether even the pessimistic end of expectancy still makes money."""
        return self.has_trades and self.expectancy_interval.low > 0

    def verdict(self) -> str:
        """One word for the table."""
        if self.status == STATUS_NO_DATA:
            return "NO DATA"
        if not self.has_trades:
            return "NO SETUPS"
        if self.survives_correction and self.profitable_lower_bound:
            return "TRADE IT"
        if self.metrics.expectancy_r > 0 and self.status == STATUS_MEASURED:
            return "PROMISING"
        if self.metrics.expectancy_r > 0:
            return "UNPROVEN"
        return "AVOID"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "group": self.group,
            "status": self.status,
            "verdict": self.verdict(),
            "bars_tested": self.bars_tested,
            "trades": self.metrics.trades,
            "wins": self.metrics.wins,
            "losses": self.metrics.losses,
            "win_rate": round(self.metrics.win_rate, 4),
            "interval_low": round(self.metrics.win_rate_interval.low, 4),
            "interval_high": round(self.metrics.win_rate_interval.high, 4),
            "family_low": round(self.family_interval.low, 4),
            "family_high": round(self.family_interval.high, 4),
            "baseline": round(self.baseline, 4),
            "expectancy_r": round(self.metrics.expectancy_r, 4),
            "expectancy_low": (
                round(low, 4) if (low := _finite(self.expectancy_interval.low)) is not None
                else None
            ),
            "total_r": round(self.metrics.total_r, 3),
            "realised_rr": round(effective_ratio(self.metrics), 3) if self.has_trades else 0.0,
            "edge_verdict": self.edge.verdict,
            "survives_correction": self.survives_correction,
            "window": self.window,
            "note": self.note,
        }


@dataclass(frozen=True)
class CurrencyReport:
    """One currency's record across every pair it appears in."""

    code: str
    trades: int
    wins: int
    win_rate: float
    expectancy_r: float
    total_r: float
    pairs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "total_r": round(self.total_r, 3),
            "pairs": list(self.pairs),
        }


@dataclass(frozen=True)
class UniverseReport:
    """The whole scan: every pair asked about, and the pooled picture."""

    reports: tuple[PairReport, ...]
    pooled: Metrics
    pooled_edge: Edge
    currencies: tuple[CurrencyReport, ...]
    family_conf: float
    confidence: float
    out_of_sample: bool
    timeframe: str

    @property
    def measured(self) -> list[PairReport]:
        return [r for r in self.reports if r.has_trades]

    @property
    def missing(self) -> list[PairReport]:
        return [r for r in self.reports if r.status == STATUS_NO_DATA]

    @property
    def tradable(self) -> list[PairReport]:
        """Pairs that survive the correction and still pay at the low bound."""
        return [r for r in self.reports if r.survives_correction and r.profitable_lower_bound]

    def ranked(self) -> list[PairReport]:
        """Best first: pairs that made money, ordered pessimistically within that.

        Two sort keys, in this order, and both are needed:

        *Did it make money at all?* A pair that lost on every trade must not
        outrank a profitable one, and it would on the second key alone — a pair
        that lost -1R eight times running has almost no variance, so its lower
        bound sits high. Low variance around a loss is not a recommendation.

        *How pessimistic is the case for it?* Among pairs that did make money,
        ranking on the point estimate would float a lucky three-trade sample to
        the top. The lower bound of expectancy favours pairs with both an edge
        and enough trades to have shown one, which is the question being asked.
        """
        return sorted(
            self.reports,
            key=lambda r: (
                r.metrics.expectancy_r > 0,
                r.expectancy_interval.low,
                r.metrics.expectancy_r,
                r.metrics.trades,
            ),
            reverse=True,
        )

    def to_dict(self) -> dict:
        return {
            "timeframe": self.timeframe,
            "out_of_sample": self.out_of_sample,
            "confidence": self.confidence,
            "family_confidence": round(self.family_conf, 6),
            "pairs": [r.to_dict() for r in self.ranked()],
            "currencies": [c.to_dict() for c in self.currencies],
            "pooled": {
                "trades": self.pooled.trades,
                "win_rate": round(self.pooled.win_rate, 4),
                "interval_low": round(self.pooled.win_rate_interval.low, 4),
                "interval_high": round(self.pooled.win_rate_interval.high, 4),
                "expectancy_r": round(self.pooled.expectancy_r, 4),
                "total_r": round(self.pooled.total_r, 3),
                "edge_verdict": self.pooled_edge.verdict,
                "edge_detail": self.pooled_edge.detail,
            },
            "counts": {
                "asked": len(self.reports),
                "with_data": len(self.reports) - len(self.missing),
                "with_trades": len(self.measured),
                "tradable": len(self.tradable),
            },
        }


def build_pair_report(
    symbol: str,
    result: BacktestResult | None,
    config: Config,
    comparisons: int,
    note: str = "",
) -> PairReport:
    """Score one pair, correcting its interval for the size of the scan."""
    conf = config.target.confidence
    fam = family_confidence(conf, comparisons)
    instrument = get_instrument(symbol)

    if result is None:
        return PairReport(
            symbol=symbol.upper(),
            group=instrument.group,
            status=STATUS_NO_DATA,
            bars_tested=0,
            trades=[],
            metrics=compute_metrics([], conf),
            edge=measure_edge(compute_metrics([], conf), config.target.min_sample),
            family_interval=Interval(0.0, 1.0, fam),
            expectancy_interval=Interval(0.0, 0.0, fam),
            family_conf=fam,
            note=note or "no candles available for this symbol",
        )

    metrics = compute_metrics(result.trades, conf)
    r_values = [t.r_multiple for t in result.trades]
    status = (
        STATUS_NO_TRADES
        if metrics.trades == 0
        else STATUS_MEASURED
        if metrics.trades >= config.target.min_sample
        else STATUS_THIN
    )
    window = (
        f"{result.first_bar[:10]} to {result.last_bar[:10]}" if result.first_bar else ""
    )
    return PairReport(
        symbol=symbol.upper(),
        group=instrument.group,
        status=status,
        bars_tested=result.bars_tested,
        trades=list(result.trades),
        metrics=metrics,
        edge=measure_edge(metrics, config.target.min_sample),
        family_interval=_wilson(metrics.wins, metrics.trades, fam),
        expectancy_interval=mean_interval(r_values, fam),
        family_conf=fam,
        note=note,
        window=window,
    )


def _wilson(wins: int, total: int, confidence: float) -> Interval:
    """Wilson interval that tolerates the extreme confidences a correction produces."""
    from .metrics import wilson_interval

    return wilson_interval(wins, total, min(confidence, 0.999999))


def analyse_universe(
    candles_by_symbol: dict[str, list[Candle]],
    config: Config,
    split: float | None = 0.7,
    symbols: list[str] | None = None,
) -> UniverseReport:
    """Measure every requested pair, and pool the result.

    ``symbols`` names the full set asked about, so pairs with no candles still
    appear in the output as NO DATA rather than vanishing. ``split`` measures on
    the out-of-sample tail; pass ``None`` to use the whole series and accept that
    the number is then in-sample.
    """
    asked = symbols or sorted(candles_by_symbol)
    comparisons = max(len(asked), 1)

    reports: list[PairReport] = []
    for symbol in asked:
        candles = candles_by_symbol.get(symbol.upper()) or candles_by_symbol.get(symbol)
        if not candles:
            reports.append(build_pair_report(symbol, None, config, comparisons))
            continue
        if len(candles) < 60:
            reports.append(
                build_pair_report(
                    symbol, None, config, comparisons,
                    note=f"only {len(candles)} bars; not enough to warm the indicators",
                )
            )
            continue
        if split:
            _, result = split_backtest(candles, symbol, config, split)
        else:
            result = run_backtest(candles, symbol, config)
        reports.append(build_pair_report(symbol, result, config, comparisons))

    pooled_trades = [t for r in reports for t in r.trades]
    pooled = compute_metrics(pooled_trades, config.target.confidence)

    return UniverseReport(
        reports=tuple(reports),
        pooled=pooled,
        pooled_edge=measure_edge(pooled, config.target.min_sample),
        currencies=tuple(currency_breakdown(reports)),
        family_conf=family_confidence(config.target.confidence, comparisons),
        confidence=config.target.confidence,
        out_of_sample=bool(split),
        timeframe=config.data.timeframe,
    )


def currency_breakdown(reports: list[PairReport]) -> list[CurrencyReport]:
    """Aggregate results by currency leg, best expectancy first.

    A currency's row counts every trade on every pair it appears in, on either
    side. That double-counts each trade across its two legs by design: the
    question being asked is "when the euro was involved, how did it go?", and a
    EURJPY trade is evidence about both.
    """
    buckets: dict[str, list[Trade]] = {}
    pairs: dict[str, set[str]] = {}
    for report in reports:
        if not report.trades:
            continue
        instrument = get_instrument(report.symbol)
        for code in instrument.currencies:
            buckets.setdefault(code, []).extend(report.trades)
            pairs.setdefault(code, set()).add(report.symbol)

    rows: list[CurrencyReport] = []
    for code, trades in buckets.items():
        wins = sum(1 for t in trades if t.r_multiple > 1e-9)
        total_r = sum(t.r_multiple for t in trades)
        rows.append(
            CurrencyReport(
                code=code,
                trades=len(trades),
                wins=wins,
                win_rate=wins / len(trades),
                expectancy_r=total_r / len(trades),
                total_r=total_r,
                pairs=tuple(sorted(pairs[code])),
            )
        )
    return sorted(rows, key=lambda r: (r.expectancy_r, r.trades), reverse=True)


# ---------------------------------------------------------------- persistence


@dataclass(frozen=True)
class Persistence:
    """Whether picking pairs on past results actually pays in the next period.

    This module's headline advice is "trade the pairs that measured well". That
    advice is worth nothing unless a pair's edge *persists* into a period it was
    not selected on — and whether it does is an empirical question, not a
    self-evident one. So it gets asked.
    """

    selected: tuple[str, ...]
    rejected: tuple[str, ...]
    everything: Metrics
    chosen: Metrics
    dropped: Metrics
    sign_agreement: float
    pairs_compared: int
    min_verdict_trades: int = 30

    @property
    def gain_r(self) -> float:
        """Expectancy the selection bought, per trade, in the later period."""
        return self.chosen.expectancy_r - self.everything.expectancy_r

    @property
    def testable(self) -> bool:
        """Whether either arm has the trades to support a comparison.

        A verdict drawn from one chosen pair is the same small-sample bravado the
        rest of this project refuses elsewhere. Without trades on both sides of
        the comparison there is no answer, only an arrangement of numbers.
        """
        return (
            self.chosen.trades >= self.min_verdict_trades
            and self.dropped.trades >= self.min_verdict_trades
        )

    @property
    def helped(self) -> bool:
        """Whether selection did anything worth the trades it gave up."""
        return self.testable and self.gain_r > 0.05

    def verdict(self) -> str:
        if not self.chosen.trades or not self.everything.trades or not self.testable:
            return "NOT TESTABLE"
        if self.helped:
            return "SELECTION HELPED"
        if self.gain_r < -0.05:
            return "SELECTION HURT"
        return "SELECTION MADE NO DIFFERENCE"


def persistence_check(
    candles_by_symbol: dict[str, list[Candle]], config: Config, min_trades: int = 5
) -> Persistence:
    """Walk-forward test of this module's own recommendation.

    Split each pair's history in half; rank on the first half only; then measure
    the second half both for every pair and for the subset the first half would
    have chosen. No outcome from the second half is visible at selection time, so
    the comparison is honest.

    A result showing no difference is not a failed run. It is the finding that
    this strategy's edge is not pair-specific — in which case filtering the
    universe costs trades and buys nothing, and the right advice is to trade the
    lot and control correlation instead.
    """
    selected: list[str] = []
    rejected: list[str] = []
    later: dict[str, list[Trade]] = {}
    agreements = 0
    compared = 0

    for symbol, candles in sorted(candles_by_symbol.items()):
        if len(candles) < 120:
            continue
        midpoint = len(candles) // 2
        early = compute_metrics(
            run_backtest(candles, symbol, config, end=midpoint - 1).trades,
            config.target.confidence,
        )
        late_result = run_backtest(candles, symbol, config, start=midpoint)
        late = compute_metrics(late_result.trades, config.target.confidence)
        later[symbol] = late_result.trades

        if early.trades and late.trades:
            compared += 1
            if (early.expectancy_r > 0) == (late.expectancy_r > 0):
                agreements += 1

        if early.trades >= min_trades and early.expectancy_r > 0:
            selected.append(symbol)
        else:
            rejected.append(symbol)

    def _pool(names: list[str]) -> Metrics:
        return compute_metrics(
            [t for name in names for t in later.get(name, [])], config.target.confidence
        )

    return Persistence(
        selected=tuple(selected),
        rejected=tuple(rejected),
        everything=_pool(selected + rejected),
        chosen=_pool(selected),
        dropped=_pool(rejected),
        sign_agreement=agreements / compared if compared else 0.0,
        pairs_compared=compared,
        min_verdict_trades=config.target.min_sample,
    )


def format_persistence(result: Persistence, width: int = 100) -> str:
    """Render the walk-forward answer, including when the answer is 'no difference'."""
    bar = "=" * width
    lines = [
        bar,
        "  DOES PICKING PAIRS ACTUALLY HELP?",
        bar,
        "",
        "  Each pair's history is split in half. Pairs are chosen on the first half only,",
        "  then both the full universe and the chosen subset are measured on the second.",
        "  Nothing from the second half is visible when the choice is made.",
        "",
        f"  Chosen on the first half: {len(result.selected)} pair(s). "
        f"Rejected: {len(result.rejected)}.",
        "",
        f"  {'second half':<24} {'trades':>7} {'win rate':>9} {'exp R':>9} {'total R':>10}",
        "  " + "-" * 62,
    ]
    for label, metrics in (
        ("trade everything", result.everything),
        ("trade the chosen", result.chosen),
        ("trade the rejected", result.dropped),
    ):
        if metrics.is_empty:
            lines.append(f"  {label:<24} {'-':>7}")
            continue
        lines.append(
            f"  {label:<24} {metrics.trades:>7} {metrics.win_rate:>9.1%} "
            f"{metrics.expectancy_r:>+8.2f}R {metrics.total_r:>+9.1f}R"
        )

    lines += [
        "",
        f"  Expectancy the selection bought: {result.gain_r:+.2f}R per trade",
        f"  Sign of expectancy carried over on {result.sign_agreement:.0%} of "
        f"{result.pairs_compared} pair(s) — chance alone gives 50%",
        "",
        f"  {result.verdict()}",
    ]
    if result.verdict() == "NOT TESTABLE":
        lines += [
            f"    Not enough trades on both sides to compare: {result.chosen.trades} on the",
            f"    chosen pairs and {result.dropped.trades} on the rejected ones, against a",
            f"    {result.min_verdict_trades}-trade minimum. Test a longer history — a verdict",
            "    drawn from a handful of trades would be the thing this tool exists to avoid.",
        ]
    elif result.verdict() == "SELECTION MADE NO DIFFERENCE":
        lines += [
            "    On this data the edge is not pair-specific. Filtering the universe would",
            "    cost you trades and buy nothing, so trade the lot and spend the effort on",
            "    correlation instead — four euro longs is one bet, whatever the table says.",
        ]
    elif result.verdict() == "SELECTION HURT":
        lines += [
            "    Choosing on past results did worse than trading everything. Past per-pair",
            "    performance is noise here; do not filter on it.",
        ]
    elif result.helped:
        lines += [
            "    Selection carried forward on this data. Re-run it before trusting it on",
            "    another period: an effect this size can appear and vanish with the sample.",
        ]
    lines.append(bar)
    return "\n".join(lines)


# ------------------------------------------------------------------ rendering


def _pct(value: float) -> str:
    return f"{value:.0%}"


def format_universe(report: UniverseReport, config: Config, width: int = 100) -> str:
    """Render the per-pair table and the verdicts that go with it."""
    bar = "=" * width
    counts = report.to_dict()["counts"]
    sample_label = "out-of-sample" if report.out_of_sample else "full series (in-sample)"

    lines = [
        bar,
        f"  WIN RATE BY PAIR  -  {counts['asked']} instrument(s) on {report.timeframe}, "
        f"{sample_label}",
        bar,
        "",
        f"  {counts['with_data']} had data, {counts['with_trades']} produced setups, "
        f"{counts['tradable']} survive the multiple-comparison correction.",
        "",
        f"  {'pair':<9} {'grp':<8} {'trades':>6} {'win':>6} {'chance':>7} "
        f"{'95% interval':>14} {'corrected':>14} {'exp R':>7} {'low':>7}  verdict",
        "  " + "-" * (width - 4),
    ]

    for row in report.ranked():
        m = row.metrics
        if not row.has_trades:
            lines.append(
                f"  {row.symbol:<9} {row.group:<8} {0:>6} {'-':>6} {'-':>7} "
                f"{'-':>14} {'-':>14} {'-':>7} {'-':>7}  {row.verdict()}"
            )
            continue
        raw = f"{_pct(m.win_rate_interval.low)}-{_pct(m.win_rate_interval.high)}"
        fam = f"{_pct(row.family_interval.low)}-{_pct(row.family_interval.high)}"
        low = _finite(row.expectancy_interval.low)
        low_text = f"{low:+6.2f}R" if low is not None else f"{'n/a':>7}"
        lines.append(
            f"  {row.symbol:<9} {row.group:<8} {m.trades:>6} {m.win_rate:>6.0%} "
            f"{row.baseline:>7.0%} {raw:>14} {fam:>14} "
            f"{m.expectancy_r:>+6.2f}R {low_text}  {row.verdict()}"
        )

    lines += [
        "",
        f"  'chance' is the win rate a coin flip gives at the ratio those trades actually",
        f"  reached. 'corrected' widens the interval to {report.family_conf:.4%} per pair so that all",
        f"  {counts['asked']} intervals hold together at {report.confidence:.0%} — the price of having looked at",
        f"  {counts['asked']} pairs before choosing one. A pair is only TRADE IT if its corrected low",
        "  beats chance and its expectancy stays positive at the low bound.",
        "",
    ]

    lines += _pooled_block(report, config)
    lines += _currency_block(report)
    lines += _recommendation_block(report, config)
    lines.append(bar)
    return "\n".join(lines)


def _pooled_block(report: UniverseReport, config: Config) -> list[str]:
    """Everything pooled — the honest headline number for the whole universe."""
    pooled = report.pooled
    if pooled.is_empty:
        return ["  POOLED: no trades across any pair. Nothing to measure.", ""]
    interval = pooled.win_rate_interval
    risk = config.account.balance * config.account.risk_per_trade_pct / 100.0
    return [
        "  POOLED ACROSS EVERY PAIR",
        f"    Trades          {pooled.trades}",
        f"    Win rate        {pooled.win_rate:.1%}  "
        f"({interval.low:.1%}-{interval.high:.1%} at {report.confidence:.0%})",
        f"    Expectancy      {pooled.expectancy_r:+.2f}R per trade",
        f"    Total           {pooled.total_r:+.1f}R  "
        f"= {pooled.total_r * risk:+,.0f} {config.account.currency} "
        f"at {config.account.risk_per_trade_pct:.1f}% risk",
        f"    Worst drawdown  {pooled.max_drawdown_r:.1f}R",
        f"    Edge            {report.pooled_edge.verdict}",
        "",
        "    Pooling is fair here — the strategy's parameters come from config and are",
        "    not fitted per pair — but these trades are not independent: pairs sharing a",
        "    currency move together, so the interval above is narrower than the truth.",
        "",
    ]


def _currency_block(report: UniverseReport) -> list[str]:
    """Per-currency table: which legs paid, across every pair they appear in."""
    if not report.currencies:
        return []
    lines = [
        "  BY CURRENCY LEG  (each trade counted under both its currencies)",
        f"    {'ccy':<5} {'trades':>7} {'win rate':>9} {'exp R':>8} {'total R':>9}  pairs",
        "    " + "-" * 76,
    ]
    for row in report.currencies:
        shown = ", ".join(row.pairs[:4])
        if len(row.pairs) > 4:
            shown += f" +{len(row.pairs) - 4}"
        lines.append(
            f"    {row.code:<5} {row.trades:>7} {row.win_rate:>9.0%} "
            f"{row.expectancy_r:>+7.2f}R {row.total_r:>+8.1f}R  {shown}"
        )
    lines.append("")
    return lines


def _recommendation_block(report: UniverseReport, config: Config) -> list[str]:
    """What to actually do with all this — the point of the exercise."""
    tradable = report.tradable
    ranked = [r for r in report.ranked() if r.has_trades and r.metrics.expectancy_r > 0]
    any_trades = [r for r in report.ranked() if r.has_trades]

    lines = ["  WHAT TO DO WITH THIS"]
    if tradable:
        names = ", ".join(r.symbol for r in tradable)
        lines += [
            f"    {len(tradable)} pair(s) cleared every test: {names}",
            "    Before you narrow your universe to them, check that picking pairs on past",
            "    results carries forward at all — run `pairs --persistence`. If it does not,",
            "    filtering costs you trades and buys nothing.",
            "    Re-run this monthly either way: an edge that survives a correction today can",
            "    still decay, and this is how you find out.",
        ]
    elif ranked:
        best = ranked[0]
        lines += [
            "    No pair survives the correction on this sample. That is the honest answer,",
            "    not a broken run — it usually means the sample is too short, not that every",
            "    pair is worthless.",
            f"    Best of the profitable ones is {best.symbol}: {best.metrics.win_rate:.0%} over "
            f"{best.metrics.trades} trades, expectancy {best.metrics.expectancy_r:+.2f}R,",
            f"    corrected interval {best.family_interval.low:.0%}-{best.family_interval.high:.0%} "
            f"against a {best.baseline:.0%} chance baseline.",
            "    Trade it small, or get more history, before treating it as an edge.",
        ]
    elif any_trades:
        lines += [
            f"    Not one of the {len(any_trades)} pairs that produced setups made money on this",
            "    sample. Do not go looking for the least-bad row — there is no trade here.",
            "    Either the history is too short to judge, or these rules do not fit this",
            "    period. Lengthen the history before drawing either conclusion.",
        ]
    else:
        lines += [
            "    No pair produced a single setup. Either the history is too short, or the",
            f"    {config.strategy.min_confluence:.0%} confluence threshold is above what this data offers.",
            "    Run `calibrate` to see the trade-off before lowering it.",
        ]

    losing = [r for r in ranked if r.metrics.expectancy_r < 0 and r.metrics.trades >= 5]
    if losing:
        lines.append(
            f"    Negative expectancy on {len(losing)} pair(s): "
            f"{', '.join(r.symbol for r in losing[:8])}"
            f"{' and others' if len(losing) > 8 else ''}."
        )
    if report.missing:
        names = ", ".join(r.symbol for r in report.missing[:10])
        more = f" and {len(report.missing) - 10} more" if len(report.missing) > 10 else ""
        lines.append(
            f"    {len(report.missing)} pair(s) had no data and are unmeasured, not cleared: "
            f"{names}{more}."
        )
    lines.append("")
    return lines
