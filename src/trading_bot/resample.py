"""Timeframe aggregation.

The higher-timeframe bias is the heaviest-weighted confluence check, so how it
is derived matters. Bars are aggregated into fixed UTC buckets, and — critically
— an HTF bar is only visible once it has *closed*. Letting a strategy see the
still-forming 4-hour bar that its own 1-hour decision bar sits inside is a
look-ahead leak that would inflate every result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .errors import DataError
from .models import Candle, Timeframe


def bucket_start(moment: datetime, minutes: int) -> datetime:
    """Floor a timestamp to the start of its bucket, anchored at the UTC epoch."""
    if moment.tzinfo is None:
        raise DataError("cannot bucket a naive timestamp")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = int((moment - epoch).total_seconds() // 60)
    return epoch + timedelta(minutes=elapsed - (elapsed % minutes))


def resample(candles: list[Candle], target: Timeframe) -> list[Candle]:
    """Aggregate candles into a higher timeframe.

    Returns only complete buckets that could be assembled from the input; a
    trailing partial bucket is dropped rather than emitted half-formed.
    """
    if not candles:
        return []
    minutes = target.minutes
    source_minutes = 0
    if len(candles) >= 2:
        source_minutes = int((candles[1].timestamp - candles[0].timestamp).total_seconds() // 60)
        if source_minutes > 0 and minutes < source_minutes:
            raise DataError(
                f"cannot resample {source_minutes}-minute bars up to {minutes}-minute bars: "
                "the target timeframe is smaller than the source"
            )

    buckets: dict[datetime, list[Candle]] = {}
    order: list[datetime] = []
    for candle in candles:
        key = bucket_start(candle.timestamp, minutes)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(candle)

    out: list[Candle] = []
    for key in order:
        group = buckets[key]
        out.append(
            Candle(
                timestamp=key,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
            )
        )
    # The last bucket is complete only if the final source bar closes at or after
    # the bucket's end. The source bar's own length is what decides that, so it
    # is inferred from the series rather than assumed — assuming one minute
    # discards a perfectly complete bucket on hourly input.
    if out:
        bucket_end = order[-1] + timedelta(minutes=minutes)
        last_close = candles[-1].timestamp + timedelta(minutes=source_minutes or minutes)
        if last_close < bucket_end:
            out.pop()
    return out


def htf_closed_before(
    candles: list[Candle], index: int, target: Timeframe
) -> list[Candle]:
    """Higher-timeframe bars that had already closed at ``candles[index]``.

    This is the function that keeps the HTF bias honest. It resamples only the
    visible history, then discards any HTF bar whose window has not finished by
    the decision bar's timestamp.
    """
    if index < 0 or index >= len(candles):
        raise IndexError(f"index {index} out of range for {len(candles)} candles")
    decision_time = candles[index].timestamp
    aggregated = resample(candles[: index + 1], target)
    cutoff = timedelta(minutes=target.minutes)
    return [c for c in aggregated if c.timestamp + cutoff <= decision_time]
