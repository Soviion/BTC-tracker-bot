"""Статистика цены BTC за сутки по собственной истории бота: изменение, диапазон, мини-график."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .db import Database
from .util import sparkline

DAY = 24 * 3600


@dataclass(frozen=True)
class PriceStats:
    change_pct: float
    span_hours: float  # за какой период посчитано (бот мог работать меньше суток)
    low: float
    high: float
    spark: str  # пустая строка, если данных мало для графика


def compute_stats(
    series: Sequence[tuple[int, float]],
    now: float,
    current: float,
    *,
    window: int = DAY,
    min_span: int = 1800,
    buckets: int = 24,
) -> PriceStats | None:
    """series — точки (unix-время, цена) по возрастанию. None — если наблюдений меньше 30 минут."""
    points = [(ts, p) for ts, p in series if ts >= now - window]
    if len(points) < 2:
        return None
    first_ts, first_price = points[0]
    span = now - first_ts
    if span < min_span or first_price <= 0:
        return None

    prices = [p for _, p in points] + [current]
    spark = ""
    if span >= 2 * 3600:
        width = span / buckets
        values: list[float] = []
        last, idx = first_price, 0
        for b in range(buckets):
            edge = first_ts + (b + 1) * width
            while idx < len(points) and points[idx][0] <= edge:
                last = points[idx][1]
                idx += 1
            values.append(last)
        values[-1] = current
        spark = sparkline(values)

    return PriceStats(
        change_pct=(current - first_price) / first_price * 100,
        span_hours=span / 3600,
        low=min(prices),
        high=max(prices),
        spark=spark,
    )


async def load_stats(db: Database, now: float, current: float) -> PriceStats | None:
    series = await db.price_series(int(now - DAY))
    return compute_stats(series, now, current)
