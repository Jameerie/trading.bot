"""Strategy protocol and the market context handed to strategies.

``MarketContext`` is built once per decision bar and carries every derived value
a strategy needs. It is constructed from ``candles[: index + 1]`` only, so a
strategy physically cannot see the future even if it tries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .. import indicators as ind
from ..config import Config
from ..instruments import Instrument
from ..models import Candle, Signal
from ..structure import StructureView, Trend, build_view


@dataclass(frozen=True)
class MarketContext:
    """Everything knowable at one decision bar."""

    symbol: str
    instrument: Instrument
    config: Config
    candles: tuple[Candle, ...]
    index: int
    view: StructureView
    ema_fast: float | None
    ema_slow: float | None
    ema_trend: float | None
    ema_fast_slope: float | None
    atr: float | None
    rsi: float | None
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    htf_trend: Trend = Trend.RANGE

    @property
    def candle(self) -> Candle:
        """The decision bar: the most recent *closed* candle."""
        return self.candles[self.index]

    @property
    def timestamp(self) -> datetime:
        return self.candle.timestamp

    @property
    def price(self) -> float:
        return self.candle.close

    @property
    def is_warm(self) -> bool:
        """Whether every indicator has left its warm-up period.

        Acting on a half-warmed indicator is the quiet way to get a strategy that
        behaves differently at the start of every dataset.
        """
        return None not in (
            self.ema_fast, self.ema_slow, self.ema_trend, self.atr, self.rsi, self.adx
        )


def build_context(
    candles: list[Candle],
    index: int,
    symbol: str,
    instrument: Instrument,
    config: Config,
    htf_trend: Trend = Trend.RANGE,
    cache=None,
) -> MarketContext:
    """Assemble everything knowable at ``index``.

    Without a cache, indicators and structure are recomputed over the visible
    slice ``candles[: index + 1]``. That is the reference behaviour: it cannot
    see the future because it is never given the future.

    With a ``precompute.SeriesCache``, the same values are read from series
    computed once over the full history. That is sound only because every
    quantity involved is causal and every structural item is gated on the bar at
    which it became knowable — see ``precompute`` for the argument, and
    ``tests/test_precompute.py`` for the assertion that both paths agree.

    ``candles`` on the returned context is always the visible slice either way,
    so a strategy indexing from the end (``ctx.candles[-12:]``) gets the twelve
    bars before the decision, never the twelve most recent bars in the file.
    """
    s = config.strategy
    visible = candles[: index + 1]
    last = len(visible) - 1

    if cache is not None:
        return MarketContext(
            symbol=symbol.upper(),
            instrument=instrument,
            config=config,
            candles=tuple(visible),
            index=last,
            view=cache.view_at(last),
            ema_fast=cache.ema_fast[last],
            ema_slow=cache.ema_slow[last],
            ema_trend=cache.ema_trend[last],
            ema_fast_slope=cache.ema_fast_slope[last],
            atr=cache.atr[last],
            rsi=cache.rsi[last],
            adx=cache.adx[last],
            plus_di=cache.plus_di[last],
            minus_di=cache.minus_di[last],
            htf_trend=cache.htf_trend[last],
        )

    closes = ind.closes(visible)
    fast = ind.ema(closes, s.ema_fast)
    slow = ind.ema(closes, s.ema_slow)
    trend = ind.ema(closes, s.ema_trend)
    plus_di, minus_di = ind.directional_index(visible, s.adx_period)

    return MarketContext(
        symbol=symbol.upper(),
        instrument=instrument,
        config=config,
        candles=tuple(visible),
        index=last,
        view=build_view(visible, last, s.swing_left, s.swing_right),
        ema_fast=fast[last],
        ema_slow=slow[last],
        ema_trend=trend[last],
        ema_fast_slope=ind.slope(fast, 5)[last],
        atr=ind.atr(visible, s.atr_period)[last],
        rsi=ind.rsi(closes, s.rsi_period)[last],
        adx=ind.adx(visible, s.adx_period)[last],
        plus_di=plus_di[last],
        minus_di=minus_di[last],
        htf_trend=htf_trend,
    )


class Strategy(Protocol):
    """A strategy turns a market context into a signal, or into nothing.

    Returning ``None`` is the normal case and is not a failure. A selective
    system says "no setup" far more often than it says "here is one".
    """

    name: str

    def evaluate(self, context: MarketContext) -> Signal | None:
        ...
