"""Forward predictions, and the ledger that keeps score of them.

A backtest replays trades whose outcome is already in the file. It is a
calibration exercise and nothing more — useful for estimating how often a setup
like this one has worked, worthless as evidence that the tool can call the next
one. Quoting it as a track record is the central dishonesty this project exists
to avoid.

So this module draws the line explicitly:

* A **prediction** is a claim made at a bar close about bars that do not exist
  yet: *price will reach the take profit before the stop loss, entering at the
  next bar's open, within N bars.* It is timestamped, it has a deadline, and it
  is written down before the outcome is knowable.
* A **base rate** is the measured frequency of that claim coming true on
  history, with its sample size and confidence interval attached. It is an
  estimate carried alongside the prediction, never a promise about it — and
  when there is no sample worth the name, this module says so instead of
  inventing a number.
* The **forward scoreboard** counts only predictions resolved after they were
  made. That number starts at zero on the day you install this, which is
  exactly as it should be: nobody has a track record they have not yet earned.

Resolution reuses ``backtest.simulate_trade``, so a live prediction is settled
by the identical rule — stop wins a tied bar, gaps fill at the open, costs
charged — that produced the base rate. If the two ever diverged, the base rate
would be describing a different game from the one being played.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .backtest import run_backtest, simulate_trade
from .clock import Clock, humanise_delta
from .config import Config
from .errors import DataError
from .instruments import Instrument, get_instrument
from .metrics import Interval, Metrics, compute_metrics, measure_edge, wilson_interval
from .models import Candle, Direction, Outcome, Signal, Timeframe, Trade, utc_now
from .sessions import is_weekend

# A prediction whose base rate rests on fewer than this many past occurrences is
# reported as unmeasured. Lower than target.min_sample on purpose: a per-pair
# base rate is a weaker claim than a "this strategy works" verdict, and refusing
# to show anything below 30 would leave most pairs with no estimate at all. The
# interval is always printed beside it, so a thin sample announces its own width.
MIN_BASE_RATE_SAMPLE = 8


def bars_to_time(start: datetime, bars: int, timeframe: Timeframe) -> datetime:
    """Walk ``bars`` forward in market time, skipping the weekend shutdown.

    A 200-bar H1 horizon is not 200 calendar hours: the market closes on Friday
    evening and does not reopen until Sunday night, so the deadline a user needs
    on their calendar is later than naive arithmetic gives. Walking bar by bar
    and skipping closed hours is exact, and cheap at these sizes.
    """
    step = timedelta(minutes=timeframe.minutes)
    cursor = start
    remaining = bars
    # Bound the walk so a pathological input cannot spin: even skipping every
    # weekend, four times the bar count is more than enough calendar steps.
    for _ in range(max(bars * 4, 8)):
        if remaining <= 0:
            break
        cursor += step
        if not is_weekend(cursor):
            remaining -= 1
    return cursor


@dataclass(frozen=True)
class BaseRate:
    """How often a claim like this one has come true, measured on history."""

    symbol: str
    sample: int
    win_rate: float
    interval: Interval
    expectancy_r: float
    average_rr: float
    source: str
    out_of_sample: bool

    @property
    def is_measured(self) -> bool:
        """Whether the sample is large enough to quote at all."""
        return self.sample >= MIN_BASE_RATE_SAMPLE

    @property
    def headline(self) -> str:
        if not self.is_measured:
            return (
                f"no measured base rate for {self.symbol} — only {self.sample} comparable "
                f"setup(s) in {self.source}. This prediction is unscored."
            )
        return (
            f"{self.win_rate:.0%} of {self.sample} comparable {self.symbol} setups reached "
            f"target before stop ({self.interval.low:.0%}-{self.interval.high:.0%} at "
            f"{self.interval.confidence:.0%} confidence)"
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "sample": self.sample,
            "win_rate": round(self.win_rate, 4),
            "interval_low": round(self.interval.low, 4),
            "interval_high": round(self.interval.high, 4),
            "expectancy_r": round(self.expectancy_r, 4),
            "average_rr": round(self.average_rr, 3),
            "source": self.source,
            "out_of_sample": self.out_of_sample,
            "measured": self.is_measured,
        }


def empty_base_rate(symbol: str, source: str, confidence: float = 0.95) -> BaseRate:
    """A base rate for a pair with no history to measure — honestly empty."""
    return BaseRate(
        symbol=symbol.upper(),
        sample=0,
        win_rate=0.0,
        interval=Interval(0.0, 1.0, confidence),
        expectancy_r=0.0,
        average_rr=0.0,
        source=source,
        out_of_sample=False,
    )


def base_rate_from_metrics(
    symbol: str, metrics: Metrics, source: str, out_of_sample: bool
) -> BaseRate:
    """Wrap measured metrics as the base rate for a pair."""
    return BaseRate(
        symbol=symbol.upper(),
        sample=metrics.trades,
        win_rate=metrics.win_rate,
        interval=metrics.win_rate_interval,
        expectancy_r=metrics.expectancy_r,
        average_rr=metrics.average_rr_planned,
        source=source,
        out_of_sample=out_of_sample,
    )


def measure_base_rate(
    candles: list[Candle], symbol: str, config: Config, split: float | None = None
) -> BaseRate:
    """Measure how often this pair's setups have paid, on the history to hand.

    ``split`` restricts the measurement to the tail of the series. That is the
    honest choice when the same history was used to pick a threshold, and it is
    unnecessary when it was not — strategy parameters here come from config and
    are not fitted per pair, so a whole-window measurement is not overfitted, it
    is simply a small sample. Either way the label travels with the number.
    """
    if len(candles) < 60:
        return empty_base_rate(symbol, "too little history to measure", config.target.confidence)

    start = int(len(candles) * split) if split else None
    result = run_backtest(candles, symbol, config, start=start)
    metrics = compute_metrics(result.trades, config.target.confidence)
    window = (
        f"{result.first_bar[:10]} to {result.last_bar[:10]}"
        if result.first_bar
        else "no bars tested"
    )
    source = (
        f"{'out-of-sample ' if split else ''}{result.bars_tested} {config.data.timeframe} "
        f"bars, {window}"
    )
    return base_rate_from_metrics(symbol, metrics, source, out_of_sample=bool(split))


@dataclass(frozen=True)
class Prediction:
    """A falsifiable claim about bars that have not happened yet."""

    signal: Signal
    made_at: datetime
    entry_deadline: datetime
    resolve_by: datetime
    horizon_bars: int
    entry_window_bars: int
    base_rate: BaseRate

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def claim(self) -> str:
        """The prediction in one sentence, stated so it can be proved wrong."""
        side = "rises to" if self.signal.direction is Direction.LONG else "falls to"
        against = "falling to" if self.signal.direction is Direction.LONG else "rising to"
        return (
            f"{self.symbol} {side} {self.signal.take_profit} before {against} "
            f"{self.signal.stop_loss}, entered at the next bar's open"
        )

    def deadline_text(self, clock: Clock) -> str:
        return clock.stamp(self.resolve_by)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utc_now()) > self.resolve_by

    def entry_window_open(self, now: datetime | None = None) -> bool:
        """Whether the entry is still worth taking, or the moment has passed."""
        return (now or utc_now()) <= self.entry_deadline

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "made_at": self.made_at.isoformat(),
            "entry_deadline": self.entry_deadline.isoformat(),
            "resolve_by": self.resolve_by.isoformat(),
            "horizon_bars": self.horizon_bars,
            "entry_window_bars": self.entry_window_bars,
            "base_rate": self.base_rate.to_dict(),
        }


def build_prediction(
    signal: Signal, config: Config, base_rate: BaseRate | None = None
) -> Prediction:
    """Turn a signal into a dated, falsifiable prediction.

    The two deadlines come straight from the backtest config rather than from
    anything new, because they must be the same numbers the base rate was
    measured under: ``entry_expiry_bars`` for how long the entry stays valid and
    ``max_bars_in_trade`` for how long the claim has to come true.
    """
    timeframe = signal.timeframe
    entry_bars = config.backtest.entry_expiry_bars
    horizon = config.backtest.max_bars_in_trade
    return Prediction(
        signal=signal,
        made_at=signal.issued_at,
        entry_deadline=bars_to_time(signal.issued_at, entry_bars, timeframe),
        resolve_by=bars_to_time(signal.issued_at, horizon, timeframe),
        horizon_bars=horizon,
        entry_window_bars=entry_bars,
        base_rate=base_rate
        or empty_base_rate(signal.symbol, "not measured", config.target.confidence),
    )


# --------------------------------------------------------------- settlement


@dataclass(frozen=True)
class Settlement:
    """What actually happened to a prediction, once the market said."""

    symbol: str
    resolved: bool
    outcome: Outcome | None
    exit_price: float | None
    exit_time: datetime | None
    r_multiple: float | None
    bars_seen: int
    note: str
    # The simulated trade behind a resolution, and where in ``candles`` it
    # filled, so the ledger can keep the bars from fill to exit with the close.
    trade: Trade | None = None
    entry_index: int | None = None


def settle(
    prediction_signal: Signal,
    candles: list[Candle],
    config: Config,
    instrument: Instrument | None = None,
) -> Settlement:
    """Resolve a prediction against candles that arrived after it was made.

    ``candles`` must contain the decision bar — the one the signal was issued on
    — and whatever has closed since. The decision bar is located by timestamp
    rather than by position so that a shifted or re-fetched window cannot
    silently settle a prediction against the wrong bar.

    A prediction is left open, not force-closed, when neither barrier has been
    touched and the horizon has not elapsed. Marking a still-running trade as
    expired would quietly convert an unfinished claim into a scored one.
    """
    instrument = instrument or get_instrument(prediction_signal.symbol)
    issued = prediction_signal.issued_at

    index = next(
        (i for i, candle in enumerate(candles) if candle.timestamp == issued), None
    )
    if index is None:
        return Settlement(
            prediction_signal.symbol, False, None, None, None, None, 0,
            "the bar this prediction was made on is not in the data supplied",
        )

    available = len(candles) - index - 1
    if available < 1:
        return Settlement(
            prediction_signal.symbol, False, None, None, None, None, 0,
            "no bar has closed since the prediction was made",
        )

    trade = simulate_trade(candles, prediction_signal, index, config, instrument)
    if trade is None:
        return Settlement(
            prediction_signal.symbol, False, None, None, None, None, available,
            "no fillable bar after the decision bar",
        )

    horizon = config.backtest.max_bars_in_trade
    if trade.outcome is Outcome.EXPIRED and available <= horizon:
        # Ran out of *data*, not of time. The claim is still live.
        return Settlement(
            prediction_signal.symbol, False, None, None, None, None, available,
            f"still open — {available} of {horizon} bars elapsed, neither level touched",
        )

    return Settlement(
        symbol=prediction_signal.symbol,
        resolved=True,
        outcome=trade.outcome,
        exit_price=trade.exit_price,
        exit_time=trade.exit_time,
        r_multiple=trade.r_multiple,
        bars_seen=trade.bars_held,
        note=(
            "hit the take profit" if trade.outcome is Outcome.WIN
            else "hit the stop loss" if trade.outcome is Outcome.LOSS
            else f"closed on the {horizon}-bar horizon without touching either level"
        ),
        trade=trade,
        entry_index=index + 1,
    )


def resolve_open_predictions(journal, source, config: Config, limit: int = 200) -> list[dict]:
    """Settle every open journal entry against fresh candles.

    This is what turns the journal from a list of opinions into a scoreboard.
    Each still-open prediction is re-checked against real bars, and the ones the
    market has answered are closed at the price the rules say they closed at —
    not at a price chosen after the fact.

    Returns one report dict per entry examined, so the caller can print what
    happened rather than silently mutating the log.

    The close carries the simulator's own R — measured from the cost-adjusted
    fill, so never more flattering than the plan — and the bars from fill to
    exit, which is what lets the ledger show what happened rather than only
    that it did.
    """
    from .journal import _signal_from_dict  # local: journal imports this module's types
    from .ledger import outcome_detail  # local: the ledger imports this module

    reports: list[dict] = []
    timeframe = Timeframe.parse(config.data.timeframe)

    for entry in journal.open_entries()[:limit]:
        symbol = entry.symbol
        try:
            signal = _signal_from_dict(entry.signal)
        except (KeyError, ValueError, DataError) as exc:
            reports.append({"id": entry.entry_id, "symbol": symbol, "status": "unreadable",
                            "detail": str(exc)})
            continue

        try:
            candles = source.fetch(symbol, timeframe, config.data.lookback_bars)
        except Exception as exc:  # data sources raise their own error types
            reports.append({"id": entry.entry_id, "symbol": symbol, "status": "no data",
                            "detail": str(exc)})
            continue

        outcome = settle(signal, candles, config)
        if not outcome.resolved:
            reports.append({"id": entry.entry_id, "symbol": symbol, "status": "open",
                            "detail": outcome.note})
            continue

        detail = (
            outcome_detail(outcome.trade, candles, outcome.entry_index)
            if outcome.trade is not None and outcome.entry_index is not None
            else None
        )
        journal.close(
            entry.entry_id,
            exit_price=outcome.exit_price,
            closed_at=outcome.exit_time,
            note=f"auto-resolved: {outcome.note}",
            r_multiple=outcome.r_multiple,
            detail=detail,
        )
        reports.append({
            "id": entry.entry_id,
            "symbol": symbol,
            "status": "resolved",
            "outcome": outcome.outcome.value if outcome.outcome else None,
            "r_multiple": outcome.r_multiple,
            "detail": outcome.note,
        })
    return reports


# -------------------------------------------------------------- scoreboard


@dataclass(frozen=True)
class Scoreboard:
    """The forward record: predictions made, then resolved, in that order."""

    made: int
    resolved: int
    still_open: int
    metrics: Metrics
    first_prediction: datetime | None
    confidence: float

    @property
    def has_verdict(self) -> bool:
        return self.resolved >= MIN_BASE_RATE_SAMPLE

    def summary(self, clock: Clock) -> list[str]:
        """Plain lines describing the forward record, without overclaiming it."""
        if self.made == 0:
            return [
                "No predictions on record yet.",
                "Run a scan: every signal it issues is written down before the outcome",
                "is knowable, and that is the only record this tool will ever claim.",
            ]
        started = (
            f" since {clock.stamp(self.first_prediction)}" if self.first_prediction else ""
        )
        lines = [
            f"{self.made} prediction(s) made{started}",
            f"{self.resolved} resolved, {self.still_open} still open",
        ]
        if self.resolved == 0:
            lines.append(
                "Nothing has resolved yet, so there is no forward win rate to report."
            )
            return lines

        interval = self.metrics.win_rate_interval
        edge = measure_edge(self.metrics, MIN_BASE_RATE_SAMPLE)
        lines += [
            f"Forward win rate {self.metrics.win_rate:.1%} "
            f"({self.metrics.wins}W / {self.metrics.losses}L), "
            f"{interval.confidence:.0%} interval {interval.low:.1%}-{interval.high:.1%}",
            f"Expectancy {self.metrics.expectancy_r:+.2f}R per prediction, "
            f"{self.metrics.total_r:+.1f}R total",
            f"Edge over chance: {edge.verdict}",
        ]
        if not self.has_verdict:
            lines.append(
                f"{self.resolved} resolved prediction(s) is too few to mean anything. "
                f"The interval above is the honest width of what you know."
            )
        return lines


def scoreboard(journal, confidence: float = 0.95) -> Scoreboard:
    """Build the forward record from the journal.

    Deliberately separate from any backtest. A backtest cannot contribute a
    single trade to this number, because a backtest never made a prediction.
    """
    entries = journal.read()
    closed = [e for e in entries if not e.is_open]
    made_at = [
        datetime.fromisoformat(e.issued_at) for e in entries if e.issued_at
    ]
    return Scoreboard(
        made=len(entries),
        resolved=len(closed),
        still_open=len(entries) - len(closed),
        metrics=journal.live_metrics(confidence),
        first_prediction=min(made_at) if made_at else None,
        confidence=confidence,
    )


# ----------------------------------------------------------------- rendering


def format_prediction(prediction: Prediction, clock: Clock, now: datetime | None = None) -> list[str]:
    """The prediction block for a signal card: the claim, the clock, the odds."""
    now = now or utc_now()
    signal = prediction.signal
    lines = [
        "  THE PREDICTION",
        f"    Claim        {prediction.claim}",
        f"    Made at      {clock.stamp(prediction.made_at)}",
        f"    Enter by     {clock.stamp(prediction.entry_deadline)} "
        f"({prediction.entry_window_bars} bars) — after that the setup is stale",
        f"    Resolves by  {clock.stamp(prediction.resolve_by)} "
        f"({prediction.horizon_bars} bars, {humanise_delta(prediction.resolve_by - now)})",
    ]

    from .playbook import wrap

    rate = prediction.base_rate
    if rate.is_measured:
        lines += wrap(rate.headline, indent=" " * 17, first="    Base rate    ")
        lines += wrap(f"measured on {rate.source}", indent=" " * 17)
        if rate.interval.low < 0.5:
            lines.append(
                f"                 the low end of that interval is {rate.interval.low:.0%}: "
                f"most of these lose, and the {signal.risk_reward:.1f}R payout is what makes"
            )
            lines.append(
                "                 the set profitable, not the frequency of being right"
            )
    else:
        lines += wrap(rate.headline, indent=" " * 17, first="    Base rate    ")
        lines += wrap(
            "take it as an untested idea, and size it as one", indent=" " * 17
        )
    return lines
