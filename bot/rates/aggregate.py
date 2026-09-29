"""Сведение котировок с нескольких источников в одну цену (медиана + отсев выбросов)."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Aggregate:
    price: float
    used: dict[str, float]
    rejected: dict[str, float]
    spread_pct: float  # разброс между использованными источниками, % от цены


def aggregate_prices(
    prices: Mapping[str, float],
    *,
    reference: float | None = None,
    max_dev_pct: float = 3.0,
) -> Aggregate | None:
    """Медиана по источникам. Значения, отклонившиеся от медианы больше `max_dev_pct`%, отбрасываются.

    * ≥3 источников: отсев выбросов относительно медианы.
    * 2 источника, расходятся сильно: верим тому, что ближе к `reference` (прошлой цене).
    * 1 источник: берём как есть (защиту от скачков даёт RateService).
    """
    if not prices:
        return None
    items = dict(prices)
    values = list(items.values())
    if len(items) >= 3:
        med = statistics.median(values)
        used = {k: v for k, v in items.items() if abs(v - med) / med * 100 <= max_dev_pct} or items
    elif len(items) == 2:
        a, b = values
        if abs(a - b) / max(a, b) * 100 <= max_dev_pct or reference is None:
            used = items
        else:
            best = min(items, key=lambda k: abs(items[k] - reference))
            used = {best: items[best]}
    else:
        used = items

    price = statistics.median(used.values())
    spread = (max(used.values()) - min(used.values())) / price * 100
    return Aggregate(
        price=price,
        used=used,
        rejected={k: v for k, v in items.items() if k not in used},
        spread_pct=spread,
    )
