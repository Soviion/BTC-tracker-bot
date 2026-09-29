"""Алерты по цене BTC: создание, проверка срабатывания, повторяющиеся алерты."""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from aiogram.types import InlineKeyboardMarkup

from .db import Alert, Database
from .notify import Notifier, SendResult

log = logging.getLogger(__name__)

REARM_HYSTERESIS_PCT = 0.25  # после срабатывания повторный алерт «заряжается», когда цена отошла на 0.25% от цели
MIN_TARGET_RATIO = 0.05  # защита от опечаток: цель в пределах от 5% до 2000% текущей цены
MAX_TARGET_RATIO = 20.0
SAME_PRICE_EPS_PCT = 0.01


class CreateStatus(enum.Enum):
    OK = "ok"
    DUPLICATE = "duplicate"
    LIMIT = "limit"
    NO_PRICE = "no_price"
    OUT_OF_RANGE = "out_of_range"
    EQUALS_CURRENT = "equals_current"


@dataclass(frozen=True)
class CreateResult:
    status: CreateStatus
    alert: Alert | None = None


def direction_for(target: float, current: float) -> str | None:
    """`above` — ждём роста до цели, `below` — ждём падения. None — цель совпадает с текущей ценой."""
    if abs(target - current) / current * 100 < SAME_PRICE_EPS_PCT:
        return None
    return "above" if target > current else "below"


def is_hit(direction: str, target: float, price: float) -> bool:
    return price >= target if direction == "above" else price <= target


def evaluate(alerts: Sequence[Alert], price: float) -> tuple[list[Alert], list[int]]:
    """Чистая функция: (алерты, которые сработали сейчас; id алертов, которые нужно «зарядить» заново).

    `armed` — алерт ждёт пересечения уровня. Сработав, он «разряжается» и снова заряжается только когда
    цена вернулась по другую сторону уровня (с запасом REARM_HYSTERESIS_PCT) — так нет спама у самой цели.
    """
    triggered: list[Alert] = []
    rearm: list[int] = []
    margin = REARM_HYSTERESIS_PCT / 100
    for a in alerts:
        if is_hit(a.direction, a.target, price):
            if a.armed:
                triggered.append(a)
        elif not a.armed:
            back = price <= a.target * (1 - margin) if a.direction == "above" else price >= a.target * (1 + margin)
            if back:
                rearm.append(a.id)
    return triggered, rearm


class AlertService:
    def __init__(self, db: Database, *, max_per_user: int = 25) -> None:
        self.db = db
        self.max_per_user = max_per_user

    async def create(self, user_id: int, target: float, current_price: float | None) -> CreateResult:
        if current_price is None:
            return CreateResult(CreateStatus.NO_PRICE)
        target = round(target, 2)
        if not current_price * MIN_TARGET_RATIO <= target <= current_price * MAX_TARGET_RATIO:
            return CreateResult(CreateStatus.OUT_OF_RANGE)
        direction = direction_for(target, current_price)
        if direction is None:
            return CreateResult(CreateStatus.EQUALS_CURRENT)
        if await self.db.count_alerts(user_id) >= self.max_per_user:
            return CreateResult(CreateStatus.LIMIT)
        alert, created = await self.db.add_alert(user_id, target, direction, current_price)
        return CreateResult(CreateStatus.OK if created else CreateStatus.DUPLICATE, alert)

    async def toggle_repeat(self, alert_id: int, user_id: int, current_price: float | None) -> Alert | None:
        """Разовый ↔ повторяющийся. Сработавший разовый алерт этим же действием включается снова."""
        alert = await self.db.get_alert(alert_id, user_id)
        if alert is None:
            return None
        hit_now = current_price is not None and is_hit(alert.direction, alert.target, current_price)
        # Если цена уже за уровнем, «заряжаем» алерт только после её возврата — иначе он сработает сразу же.
        armed = not hit_now
        if not alert.active:
            await self.db.set_alert_mode(alert.id, repeat=True, armed=armed, active=True)
        elif alert.repeat:
            await self.db.set_alert_mode(alert.id, repeat=False, armed=alert.armed)
        else:
            await self.db.set_alert_mode(alert.id, repeat=True, armed=armed)
        return await self.db.get_alert(alert.id, user_id)


Renderer = Callable[[Alert, float], tuple[str, InlineKeyboardMarkup | None]]


async def process_alerts(
    db: Database,
    notifier: Notifier,
    render: Renderer,
    price: float,
    now: int | None = None,
) -> int:
    """Проверяет все активные алерты по свежей цене и рассылает уведомления. Возвращает число срабатываний.

    Доставка «как минимум один раз»: при временном сбое отправки алерт остаётся взведённым и
    сработает на следующем тике; окончательные ошибки (пользователь заблокировал бота) закрывают его.
    """
    now = int(time.time()) if now is None else now
    triggered, rearm = evaluate(await db.all_active_alerts(), price)
    for alert_id in rearm:
        await db.set_alert_armed(alert_id, True)

    fired = 0
    for alert in triggered:
        text, markup = render(alert, price)
        result = await notifier.send(alert.user_id, text, markup)
        if result is SendResult.RETRY:
            log.warning("алерт #%s не доставлен (временный сбой) — повторю на следующем тике", alert.id)
            continue
        await db.mark_alert_triggered(alert.id, price, now, deactivate=not alert.repeat)
        fired += 1
        log.info("алерт #%s (%s %.2f) сработал для %s, доставка: %s", alert.id, alert.direction, alert.target,
                 alert.user_id, result.value)
    return fired
