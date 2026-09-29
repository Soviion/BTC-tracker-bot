"""Фоновые циклы: опрос курсов и алерты, сводки по расписанию, слежение за здоровьем источников, уборка БД."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import keyboards, texts
from .alerts import process_alerts
from .config import Settings
from .db import Alert, Database
from .history import load_stats
from .notify import Notifier
from .rates.models import Snapshot
from .rates.service import RateService

log = logging.getLogger(__name__)

# Насколько долго данные должны отсутствовать, прежде чем владельцу придёт тревога.
BTC_DOWN_NOTIFY_AFTER = 600.0  # 10 минут — у BTC 13 источников, столько простаивать все разом не должны
FIAT_DOWN_NOTIFY_AFTER = 3 * 3600.0  # 3 часа — НБРБ обновляется раз в сутки, короткие сбои — норма
MAINTENANCE_FIRST_DELAY = 300.0
MAINTENANCE_INTERVAL = 24 * 3600.0
REPORT_CHECK_INTERVAL = 20.0


@dataclass
class HealthState:
    down_since: dict[str, float | None] = field(default_factory=lambda: {"btc": None, "fiat": None})
    notified: dict[str, bool] = field(default_factory=lambda: {"btc": False, "fiat": False})


@dataclass
class SchedulerContext:
    settings: Settings
    db: Database
    rates: RateService
    notifier: Notifier
    version: str
    health: HealthState = field(default_factory=HealthState)


def start_background_tasks(ctx: SchedulerContext) -> list[asyncio.Task]:
    """Запускает все фоновые циклы. Отменить их — обязанность вызывающего кода при остановке бота."""
    return [
        asyncio.create_task(_poll_loop(ctx), name="poll_loop"),
        asyncio.create_task(_report_loop(ctx), name="report_loop"),
        asyncio.create_task(_maintenance_loop(ctx), name="maintenance_loop"),
    ]


# --------------------------------------------------------------------------- #
# Опрос курсов, алерты, здоровье источников
# --------------------------------------------------------------------------- #
async def _poll_loop(ctx: SchedulerContext) -> None:
    while True:
        try:
            await _poll_once(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — один сбойный тик не должен останавливать бота
            log.exception("сбой в цикле опроса курсов")
        await asyncio.sleep(ctx.settings.poll_interval)


async def _poll_once(ctx: SchedulerContext) -> None:
    snap = await ctx.rates.poll()
    now = time.time()
    # Метка «жив на момент X» переживёт даже kill -9 / отключение питания — по ней при следующем
    # запуске считаем длительность простоя для стартового сообщения (см. compute_startup_gap).
    await ctx.db.set_meta("last_alive_at", repr(now))

    if snap.btc is not None:
        await ctx.db.add_price(
            int(now), snap.btc.price, snap.usd_byn,
            snap.fiat.eur if snap.fiat else None, snap.fiat.rub if snap.fiat else None,
        )
        stats = await load_stats(ctx.db, now, snap.btc.price)
        fired = await process_alerts(ctx.db, ctx.notifier, _alert_renderer(ctx, snap, stats, now), snap.btc.price, int(now))
        if fired:
            log.info("сработавших алертов на этом тике: %d", fired)

    await _check_health(ctx, snap, now)


def _alert_renderer(ctx: SchedulerContext, snap: Snapshot, stats, now: float):
    def render(alert: Alert, price: float):
        text = texts.render_alert_fired(alert, price, stats, tz=ctx.settings.tz, now=now, usd_byn=snap.usd_byn)
        return text, keyboards.alert_fired_kb(alert)

    return render


async def _check_health(ctx: SchedulerContext, snap: Snapshot, now: float) -> None:
    ages = {
        "btc": (now - snap.btc.ts) if snap.btc else math.inf,
        "fiat": (now - snap.fiat.ts) if snap.fiat else math.inf,
    }
    thresholds = {"btc": BTC_DOWN_NOTIFY_AFTER, "fiat": FIAT_DOWN_NOTIFY_AFTER}
    for kind, age in ages.items():
        threshold = thresholds[kind]
        down_since = ctx.health.down_since[kind]
        if age > threshold:
            down_since = down_since or (now - age if math.isfinite(age) else now)
            ctx.health.down_since[kind] = down_since
            if not ctx.health.notified[kind] and now - down_since >= threshold:
                minutes = max(1, round((now - down_since) / 60))
                await ctx.notifier.notify_owners(ctx.settings.owner_ids, texts.render_health_down(kind, minutes))
                ctx.health.notified[kind] = True
        else:
            if ctx.health.notified[kind] and down_since is not None:
                minutes = max(1, round((now - down_since) / 60))
                await ctx.notifier.notify_owners(ctx.settings.owner_ids, texts.render_health_up(kind, minutes))
            ctx.health.down_since[kind] = None
            ctx.health.notified[kind] = False


# --------------------------------------------------------------------------- #
# Сводки по расписанию
# --------------------------------------------------------------------------- #
async def _report_loop(ctx: SchedulerContext) -> None:
    while True:
        try:
            await _maybe_send_reports(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("сбой в цикле сводок")
        await asyncio.sleep(REPORT_CHECK_INTERVAL)


async def _maybe_send_reports(ctx: SchedulerContext, now_dt: datetime | None = None) -> None:
    now_dt = now_dt or datetime.now(ctx.settings.tz)
    grace = ctx.settings.report_grace_minutes * 60
    # Проверяем и «сегодняшнюю», и «вчерашнюю» дату для каждого слота: если бот был недоступен ровно
    # в момент смены суток, окно прощения для слота 23:59 должно пережить переход через полночь.
    for days_back in (0, 1):
        base_date = now_dt - timedelta(days=days_back)
        day = base_date.strftime("%Y-%m-%d")
        for slot in ctx.settings.report_times:
            scheduled = base_date.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
            late = (now_dt - scheduled).total_seconds()
            if not 0 <= late <= grace:
                continue  # ещё не время или уже слишком поздно (пропускаем, а не шлём сводку часы спустя)
            slot_str = slot.strftime("%H:%M")
            if not await ctx.db.claim_report(day, slot_str, int(now_dt.timestamp())):
                continue  # уже отправлено (в этом тике или до перезапуска процесса)
            await _send_report(ctx, slot_str, late_minutes=int(late // 60))


async def _send_report(ctx: SchedulerContext, slot_str: str, *, late_minutes: int) -> None:
    now = time.time()
    snap = ctx.rates.snapshot()
    if ctx.rates.fresh_btc(now) is None:
        snap = await ctx.rates.poll()  # последний шанс получить цену прямо сейчас, а не молчать до утра
    stats = await load_stats(ctx.db, now, snap.btc.price) if snap.btc else None
    text = texts.render_report(
        snap, stats, slot_label=slot_str, now=now, tz=ctx.settings.tz,
        late_minutes=late_minutes, stale_after=ctx.settings.stale_after,
    )
    recipients = await ctx.db.report_recipients(slot_str)
    result = await ctx.notifier.broadcast(recipients, text)
    log.info(
        "сводка %s: получателей %d (доставлено %d, повтор %d, ошибок %d)",
        slot_str, len(recipients), result.sent, result.retry, result.failed,
    )


# --------------------------------------------------------------------------- #
# Обслуживание БД
# --------------------------------------------------------------------------- #
async def _maintenance_loop(ctx: SchedulerContext) -> None:
    await asyncio.sleep(MAINTENANCE_FIRST_DELAY)
    while True:
        try:
            await ctx.db.prune()
            log.info("очистка старых данных в БД выполнена")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("сбой очистки БД")
        await asyncio.sleep(MAINTENANCE_INTERVAL)


# --------------------------------------------------------------------------- #
# Стартовая сводка (см. bot/main.py) — учитывает время простоя между запусками
# --------------------------------------------------------------------------- #
async def get_last_alive(db: Database) -> float | None:
    """Отметка времени последнего успешного тика опроса. None — бот запускается впервые."""
    raw = await db.get_meta("last_alive_at")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
