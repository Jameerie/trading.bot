"""The evaluation loop shared by live scanning and backtesting.

Both paths must produce identical signals from identical history — if the live
scanner and the backtester disagreed, the measured win rate would say nothing
about the signals a user actually receives. So both call ``evaluate_at``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .instruments import get_instrument
from .precompute import build_cache
from .models import Candle, Signal, Timeframe
from .resample import htf_closed_before
from .strategy import get_strategy
from .strategy.base import MarketContext, build_context
from .structure import Trend, classify_trend, swing_points


@dataclass(frozen=True)
class Evaluation:
    """The result of looking at one bar: a signal, or the reason there isn't one."""

    index: int
    signal: Signal | None
    confluence_fraction: float | None
    context: MarketContext | None

    @property
    def has_signal(self) -> bool:
        return self.signal is not None


def htf_bias(candles: list[Candle], index: int, config: Config) -> Trend:
    """Structural trend on the higher timeframe, using closed HTF bars only."""
    try:
        target = Timeframe.parse(config.data.htf_timeframe)
    except Exception:
        return Trend.RANGE
    htf = htf_closed_before(candles, index, target)
    if len(htf) < 6:
        return Trend.RANGE
    swings = swing_points(htf, config.strategy.swing_left, config.strategy.swing_right)
    return classify_trend(swings)


def evaluate_at(
    candles: list[Candle], index: int, symbol: str, config: Config, strategy=None, cache=None
) -> Evaluation:
    """Evaluate one decision bar.

    ``index`` must refer to a **closed** candle. Callers scanning live data are
    responsible for excluding the bar still forming; ``scan_latest`` does this.

    ``cache`` is an optional ``precompute.SeriesCache`` for the same candles.
    Passing one turns a whole-series walk from quadratic into linear; omitting it
    falls back to recomputing from the visible slice.
    """
    strategy = strategy or get_strategy(config.strategy.name)
    instrument = get_instrument(symbol)
    context = build_context(
        candles=candles,
        index=index,
        symbol=symbol,
        instrument=instrument,
        config=config,
        htf_trend=Trend.RANGE if cache is not None else htf_bias(candles, index, config),
        cache=cache,
    )

    signal = strategy.evaluate(context)

    # Report the score even when nothing qualified, so a user can see how close
    # the market came rather than just being told "no".
    fraction = None
    if context.is_warm:
        direction = strategy.candidate_direction(context)
        if direction is not None:
            fraction = strategy.engine.score(context, direction).fraction

    return Evaluation(index=index, signal=signal, confluence_fraction=fraction, context=context)


def scan_latest(candles: list[Candle], symbol: str, config: Config, strategy=None) -> Evaluation:
    """Evaluate the most recent closed bar — the live 'what should I do now' path."""
    if not candles:
        raise ValueError("cannot scan an empty candle series")
    return evaluate_at(candles, len(candles) - 1, symbol, config, strategy)


def scan_range(
    candles: list[Candle],
    symbol: str,
    config: Config,
    start: int | None = None,
    end: int | None = None,
    strategy=None,
) -> list[Evaluation]:
    """Evaluate every bar in a range. Used by the backtester and calibrator."""
    strategy = strategy or get_strategy(config.strategy.name)
    # Skip the indicator warm-up: the slowest EMA plus its seed window.
    warmup = max(config.strategy.ema_trend, config.strategy.adx_period * 3) + 5
    first = warmup if start is None else max(start, warmup)
    last = len(candles) - 1 if end is None else min(end, len(candles) - 1)
    cache = build_cache(candles, config)
    return [
        evaluate_at(candles, i, symbol, config, strategy, cache)
        for i in range(first, last + 1)
    ]
