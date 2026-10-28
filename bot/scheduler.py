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
from .notify import BroadcastStats, Notifier, SendResult
from .rates.models import Snapshot
from .rates.service import RateService

log = logging.getLogger(__name__)

# Насколько долго данные должны отсутствовать, прежде чем владельцу придёт тревога.
BTC_DOWN_NOTIFY_AFTER = 600.0  # 10 минут — у BTC 13 источников, столько простаивать все разом не должны
FIAT_DOWN_NOTIFY_AFTER = 3 * 3600.0  # 3 часа — НБРБ обновляется раз в сутки, короткие сбои — норма
MAINTENANCE_FIRST_DELAY = 300.0
MAINTENANCE_INTERVAL = 24 * 3600.0
REPORT_CHECK_INTERVAL = 20.0

# Стартовая сводка: после включения сервера интернет (роутер, провайдер) нередко появляется позже самого
# бота — поэтому доставку не пытаемся сделать «один раз в момент запуска», а ждём связь до этого срока.
STARTUP_REPORT_DEADLINE = 3600.0
STARTUP_REPORT_RETRY = 15.0
STARTUP_META_KEY = "last_startup_report_at"


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
    # запуске считаем длительность простоя для стартового сообщения (см. deliver_startup_report).
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
            result = await _send_report(ctx, slot_str, late_minutes=int(late // 60))
            if result.sent == 0 and result.retry > 0:
                # Никому не доставлено из-за сбоя связи с Telegram: снимаем отметку, и следующий тик
                # (через REPORT_CHECK_INTERVAL) повторит отправку — до конца окна прощения.
                await ctx.db.release_report(day, slot_str)
                log.warning("сводка %s не доставлена (нет связи с Telegram) — повторю на следующем тике", slot_str)


async def _send_report(ctx: SchedulerContext, slot_str: str, *, late_minutes: int) -> BroadcastStats:
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
    return result


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
# Стартовая сводка (запускается из bot/main.py фоновой задачей)
# --------------------------------------------------------------------------- #
async def _meta_float(db: Database, key: str) -> float | None:
    raw = await db.get_meta(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def get_last_alive(db: Database) -> float | None:
    """Отметка времени последнего тика опроса. None — бот запускается впервые."""
    return await _meta_float(db, "last_alive_at")


def start_startup_report_task(ctx: SchedulerContext, prev_alive: float | None, boot_ts: float) -> asyncio.Task:
    async def guarded() -> None:
        try:
            await deliver_startup_report(ctx, prev_alive, boot_ts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сбой стартовой сводки не должен ронять бота
            log.exception("сбой при доставке стартовой сводки")

    return asyncio.create_task(guarded(), name="startup_report")


async def deliver_startup_report(
    ctx: SchedulerContext,
    prev_alive: float | None,
    boot_ts: float,
    *,
    deadline: float = STARTUP_REPORT_DEADLINE,
    retry_every: float = STARTUP_REPORT_RETRY,
    sleep=asyncio.sleep,
    clock=time.time,
) -> bool:
    """Присылает курсы сразу после запуска бота — и после включения сервера, когда интернета ещё может
    не быть: ждёт появления данных и связи с Telegram (не дольше `deadline`), а не пробует один раз.

    Защита от спама при цикле падений — по времени последней ДОСТАВЛЕННОЙ стартовой сводки
    (STARTUP_META_KEY), а не по `last_alive_at`: иначе бот, упавший из-за отсутствия сети, при
    быстрых перезапусках навсегда «забыл» бы про сводку. Возвращает True, если доставлено хоть кому-то."""
    settings = ctx.settings
    if settings.startup_report == "off":
        return False

    last_sent = await _meta_float(ctx.db, STARTUP_META_KEY)
    if last_sent is not None and boot_ts - last_sent < settings.startup_min_gap:
        log.info("пропускаю стартовую сводку — предыдущая отправлена совсем недавно (защита от спама при перезапусках)")
        return False

    owners = set(settings.owner_ids)
    pending = set(owners)
    if settings.startup_report == "all":
        pending |= {u.user_id for u in await ctx.db.list_users() if not u.bot_blocked}
    if not pending:
        return False

    notice_sent = False
    delivered = False
    while pending and clock() - boot_ts < deadline:
        now = clock()
        snap = ctx.rates.snapshot()
        if ctx.rates.fresh_btc(now) is None:
            snap = await ctx.rates.refresh_if_stale(retry_every)  # не ждём очередного тика опроса — пробуем сами
        if ctx.rates.fresh_btc(clock()) is None:
            if not notice_sent:  # владельцу — один раз: «бот жив, но источники пока молчат»
                notice_sent = True
                await ctx.notifier.notify_owners(owners, texts.render_startup_no_data(boot_ts, settings.tz))
            await sleep(retry_every)
            continue

        now = clock()
        stats = await load_stats(ctx.db, now, snap.btc.price)
        owner_text = texts.render_startup_owner(
            snap, stats, now=now, tz=settings.tz, tz_name=settings.tz_name, prev_alive=prev_alive,
            version=ctx.version, stale_after=settings.stale_after, booted_at=boot_ts,
        )
        user_text = texts.render_startup_user(snap, stats, now=now, tz=settings.tz, stale_after=settings.stale_after)
        for uid in sorted(pending):
            result = await ctx.notifier.send(uid, owner_text if uid in owners else user_text)
            if result is SendResult.OK and not delivered:
                delivered = True
                await ctx.db.set_meta(STARTUP_META_KEY, repr(clock()))
            if result is not SendResult.RETRY:  # RETRY — временный сбой связи, остальное — окончательно
                pending.discard(uid)
        if pending:
            await sleep(retry_every)

    if pending:
        log.warning("стартовая сводка не доставлена %d получателям за %.0f мин — связь так и не появилась",
                    len(pending), deadline / 60)
    elif delivered:
        log.info("стартовая сводка доставлена")
    return delivered
