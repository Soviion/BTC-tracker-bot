"""Сборка приложения: настройки → БД → сервисы → диспетчер aiogram → фоновые циклы → polling."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import __version__, texts
from .access import AccessService
from .alerts import AlertService
from .config import ConfigError, Settings, configure_logging, load_settings
from .db import Database
from .handlers import build_router
from .history import load_stats
from .middlewares import AccessMiddleware, StateResetMiddleware
from .notify import Notifier
from .rates.http import JsonFetcher
from .rates.service import RateService
from .scheduler import SchedulerContext, get_last_alive, start_background_tasks

log = logging.getLogger(__name__)

BOT_COMMANDS = (
    ("start", "Запуск / главное меню"),
    ("rates", "Курсы сейчас"),
    ("alerts", "Мои алерты"),
    ("alert", "Быстрый алерт, напр. /alert 90000"),
    ("reports", "Время ежедневных сводок"),
    ("menu", "Главное меню"),
    ("help", "Помощь"),
    ("id", "Мой Telegram ID"),
)

# Сколько пытаться подключиться к БД при старте, прежде чем сдаться. Актуально сразу после перезагрузки
# сервера: сеть/Postgres могут подняться на несколько секунд позже самого бота (см. systemd Restart=always).
DB_CONNECT_MAX_ATTEMPTS = 60
DB_CONNECT_RETRY_DELAY = 5.0


async def _verify_token_or_exit(bot: Bot) -> None:
    """Быстрая проверка BOT_TOKEN перед долгой инициализацией (БД, опрос курсов). Неверный токен —
    единственная стартовая ошибка, повтор которой заведомо бесполезен, поэтому именно её проверяем
    заранее и выходим с понятным сообщением, а не разбираемся в этом уже после подключения к БД.
    Сетевые сбои здесь не фатальны — их подхватит собственный retry/backoff aiogram при polling."""
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        print(
            "❌ Telegram отклонил BOT_TOKEN (неверный или отозванный токен). "
            "Проверьте значение BOT_TOKEN в .env — его нужно скопировать из @BotFather целиком.",
            file=sys.stderr,
        )
        await bot.session.close()
        raise SystemExit(1) from None
    except Exception as exc:  # noqa: BLE001 — сеть/Telegram недоступны сейчас — не фатально, попробуем позже
        log.warning("не удалось сразу проверить BOT_TOKEN (%s) — продолжаю, polling попробует ещё раз", exc)
    else:
        log.info("авторизован в Telegram как @%s (id %d)", me.username, me.id)


async def _connect_db_with_retry(settings: Settings) -> Database:
    db = Database(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        ssl=settings.db_ssl,
        disable_statement_cache=settings.db_disable_statement_cache,
    )
    for attempt in range(1, DB_CONNECT_MAX_ATTEMPTS + 1):
        try:
            await db.connect()
            return db
        except ConnectionError as exc:
            if attempt == DB_CONNECT_MAX_ATTEMPTS:
                raise
            log.warning(
                "БД недоступна (попытка %d/%d): %s — повтор через %.0fс",
                attempt, DB_CONNECT_MAX_ATTEMPTS, exc, DB_CONNECT_RETRY_DELAY,
            )
            await asyncio.sleep(DB_CONNECT_RETRY_DELAY)
    raise AssertionError("unreachable")  # pragma: no cover


async def _send_startup_reports(ctx: SchedulerContext, prev_alive: float | None, now: float) -> None:
    settings = ctx.settings
    if settings.startup_report == "off":
        return
    if prev_alive is not None and now - prev_alive < settings.startup_min_gap:
        log.info("пропускаю стартовое сообщение о курсах — слишком быстрый перезапуск (защита от спама)")
        return

    snap = ctx.rates.snapshot()
    if snap.btc is None:
        await ctx.notifier.notify_owners(settings.owner_ids, texts.render_startup_no_data(now, settings.tz))
        return

    stats = await load_stats(ctx.db, now, snap.btc.price)
    owner_text = texts.render_startup_owner(
        snap, stats, now=now, tz=settings.tz, tz_name=settings.tz_name, prev_alive=prev_alive,
        version=ctx.version, stale_after=settings.stale_after,
    )
    await ctx.notifier.notify_owners(settings.owner_ids, owner_text)

    if settings.startup_report == "all":
        users = await ctx.db.list_users()
        others = [u.user_id for u in users if u.user_id not in settings.owner_ids and not u.bot_blocked]
        if others:
            user_text = texts.render_startup_user(snap, stats, now=now, tz=settings.tz, stale_after=settings.stale_after)
            await ctx.notifier.broadcast(others, user_text)


async def run() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"❌ Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    configure_logging(settings)
    log.info("BTC Tracker Bot v%s запускается…", __version__)
    if settings.setup_mode:
        log.warning("OWNER_ID не задан — бот в режиме настройки: любой /start только покажет Telegram ID отправителя")

    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(token=settings.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    await _verify_token_or_exit(bot)

    db = await _connect_db_with_retry(settings)
    await db.sync_owners(settings.owner_ids)
    log.info("подключение к БД установлено, владельцев: %d", len(settings.owner_ids))

    fetcher = JsonFetcher(timeout=settings.http_timeout, proxy=settings.api_proxy)
    rate_service = RateService(fetcher, stale_after=settings.stale_after)
    access = AccessService(db, settings.owner_ids, invite_ttl_seconds=settings.invite_ttl_hours * 3600)
    alert_service = AlertService(db, max_per_user=settings.max_alerts_per_user)
    notifier = Notifier(bot, db)

    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["access"] = access
    dp["rates"] = rate_service
    dp["alert_service"] = alert_service
    dp["notifier"] = notifier
    dp["settings"] = settings
    dp["version"] = __version__
    started_at = time.time()
    dp["started_at"] = started_at

    state_reset = StateResetMiddleware()
    access_middleware = AccessMiddleware()
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(state_reset)
        observer.outer_middleware(access_middleware)
    dp.include_router(build_router())

    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    except Exception:  # noqa: BLE001 — список команд не критичен для работы бота
        log.exception("не удалось задать список команд бота")

    log.info("первый опрос источников курсов…")
    await rate_service.poll()
    if rate_service.btc:
        log.info("BTC: %.2f USD (%d/%d источников)", rate_service.btc.price, rate_service.btc.sources_used, rate_service.btc.sources_total)
    else:
        log.warning("после первого опроса цена BTC так и не получена — проверьте интернет (python -m bot --check-apis)")

    prev_alive = await get_last_alive(db)
    ctx = SchedulerContext(settings=settings, db=db, rates=rate_service, notifier=notifier, version=__version__)
    await _send_startup_reports(ctx, prev_alive, time.time())

    tasks = start_background_tasks(ctx)
    log.info("бот запущен, начинаю polling")
    try:
        await dp.start_polling(bot)
    finally:
        log.info("остановка: завершаю фоновые задачи…")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await fetcher.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    # SystemExit (ошибка конфигурации, неверный BOT_TOKEN) намеренно не перехватывается — процесс должен
    # завершиться с ненулевым кодом, иначе systemd/мониторинг решит, что бот остановился штатно.
