"""Technical indicators as pure functions over lists of floats or Candles.

Every function returns a list the same length as its input, left-padded with
``None`` for the warm-up period. Keeping the length aligned means index ``i`` of
any indicator always refers to candle ``i`` — the alternative (trimming) is how
off-by-one look-ahead bugs get introduced.
"""

from __future__ import annotations

from .models import Candle

Series = list[float | None]


def _require(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")


def sma(values: list[float], period: int) -> Series:
    """Simple moving average."""
    _require(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: list[float], period: int) -> Series:
    """Exponential moving average, seeded with the SMA of the first window.

    Seeding with an SMA rather than the first value keeps early output from being
    dominated by a single arbitrary price.
    """
    _require(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * alpha + prev * (1 - alpha)
        out[i] = prev
    return out


def rma(values: list[float], period: int) -> Series:
    """Wilder's smoothing (used by ATR, RSI and ADX)."""
    _require(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def true_range(candles: list[Candle]) -> Series:
    """True range. The first bar has no previous close, so it stays None."""
    out: Series = [None] * len(candles)
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        out[i] = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
    return out


def atr(candles: list[Candle], period: int = 14) -> Series:
    """Average true range, Wilder-smoothed."""
    _require(period)
    tr = true_range(candles)
    trimmed = [v for v in tr if v is not None]
    smoothed = rma(trimmed, period)
    out: Series = [None] * len(candles)
    # true_range drops exactly one leading value, so shift results by one.
    for i, value in enumerate(smoothed):
        out[i + 1] = value
    return out


def rsi(values: list[float], period: int = 14) -> Series:
    """Relative strength index."""
    _require(period)
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = rma(gains, period)
    avg_loss = rma(losses, period)
    for i, (g, l) in enumerate(zip(avg_gain, avg_loss)):
        if g is None or l is None:
            continue
        # A zero average loss means an unbroken run of gains: RSI is 100 by definition.
        out[i + 1] = 100.0 if l == 0 else 100.0 - (100.0 / (1.0 + g / l))
    return out


def adx(candles: list[Candle], period: int = 14) -> Series:
    """Average directional index — trend *strength*, ignoring direction."""
    _require(period)
    n = len(candles)
    out: Series = [None] * n
    if n < period * 2 + 1:
        return out
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        c, prev = candles[i], candles[i - 1]
        up = c.high - prev.high
        down = prev.low - c.low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    sm_plus, sm_minus, sm_tr = rma(plus_dm, period), rma(minus_dm, period), rma(trs, period)
    dx: list[float] = []
    dx_index: list[int] = []
    for i in range(len(trs)):
        if sm_tr[i] is None or sm_tr[i] == 0:
            continue
        pdi = 100.0 * (sm_plus[i] or 0.0) / sm_tr[i]
        mdi = 100.0 * (sm_minus[i] or 0.0) / sm_tr[i]
        total = pdi + mdi
        dx.append(0.0 if total == 0 else 100.0 * abs(pdi - mdi) / total)
        dx_index.append(i + 1)
    smoothed = rma(dx, period)
    for j, value in enumerate(smoothed):
        if value is not None:
            out[dx_index[j]] = value
    return out


def directional_index(candles: list[Candle], period: int = 14) -> tuple[Series, Series]:
    """+DI and -DI, for callers that need trend direction as well as strength."""
    _require(period)
    n = len(candles)
    plus_out: Series = [None] * n
    minus_out: Series = [None] * n
    if n < period + 1:
        return plus_out, minus_out
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        c, prev = candles[i], candles[i - 1]
        up = c.high - prev.high
        down = prev.low - c.low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    sm_plus, sm_minus, sm_tr = rma(plus_dm, period), rma(minus_dm, period), rma(trs, period)
    for i in range(len(trs)):
        if sm_tr[i] and sm_tr[i] > 0:
            plus_out[i + 1] = 100.0 * (sm_plus[i] or 0.0) / sm_tr[i]
            minus_out[i + 1] = 100.0 * (sm_minus[i] or 0.0) / sm_tr[i]
    return plus_out, minus_out


def closes(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def highs(candles: list[Candle]) -> list[float]:
    return [c.high for c in candles]


def lows(candles: list[Candle]) -> list[float]:
    return [c.low for c in candles]


def rolling_extreme(values: list[float], period: int, mode: str = "max") -> Series:
    """Rolling max or min over a trailing window (inclusive of the current bar)."""
    _require(period)
    pick = max if mode == "max" else min
    out: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = pick(values[i - period + 1 : i + 1])
    return out


def slope(values: Series, lookback: int = 5) -> Series:
    """Per-bar change of a series over ``lookback`` bars.

    Used to ask 'is this moving average actually rising?' rather than just
    comparing two prices, which is noisy on a single bar.
    """
    _require(lookback)
    out: Series = [None] * len(values)
    for i in range(lookback, len(values)):
        a, b = values[i - lookback], values[i]
        if a is not None and b is not None:
            out[i] = (b - a) / lookback
    return out
