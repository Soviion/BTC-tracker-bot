"""Сборка приложения: настройки → БД → сервисы → диспетчер aiogram → фоновые циклы → polling."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import __version__
from .access import AccessService
from .alerts import AlertService
from .config import ConfigError, Settings, configure_logging, load_settings
from .db import Database
from .handlers import build_router
from .middlewares import AccessMiddleware, StateResetMiddleware
from .notify import Notifier
from .rates.http import JsonFetcher
from .rates.service import RateService
from .scheduler import SchedulerContext, get_last_alive, start_background_tasks, start_startup_report_task

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


async def _verify_token_or_exit(bot: Bot, *, retry_delay: float = 10.0, sleep=asyncio.sleep) -> None:
    """Проверка BOT_TOKEN до долгой инициализации (БД, опрос курсов).

    * Неверный токен — единственная стартовая ошибка, повтор которой заведомо бесполезен: выходим с
      понятным сообщением и кодом 1.
    * Нет связи с Telegram (например, сервер только что включили, а интернет ещё не поднялся) — ждём и
      повторяем, а не падаем. Это важно ещё и потому, что `start_polling` в aiogram сам вызывает
      `bot.me()` ДО своего цикла с backoff: если бы мы не заполнили этот кэш здесь, нет сети в этот
      момент означало бы падение процесса.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            me = await bot.me()  # кэшируется внутри Bot — повторный вызов в start_polling уже без сети
        except TelegramUnauthorizedError:
            print(
                "❌ Telegram отклонил BOT_TOKEN (неверный или отозванный токен). "
                "Проверьте значение BOT_TOKEN (в .env или в environment контейнера) — его нужно скопировать "
                "из @BotFather целиком.",
                file=sys.stderr,
            )
            await bot.session.close()
            raise SystemExit(1) from None
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
            if attempt == 1 or attempt % 12 == 0:  # не заваливаем лог: первая попытка и затем раз в пару минут
                log.warning("нет связи с Telegram (попытка %d): %s — повторяю каждые %.0fс", attempt, exc, retry_delay)
            await sleep(retry_delay)
        else:
            log.info("авторизован в Telegram как @%s (id %d)", me.username, me.id)
            return


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


def build_dispatcher(
    *,
    db: Database,
    access: AccessService,
    rates: RateService,
    alert_service: AlertService,
    notifier: Notifier,
    settings: Settings,
    started_at: float,
) -> Dispatcher:
    """Диспетчер aiogram со всеми зависимостями, middleware и роутерами. Вынесено из run(), чтобы тесты
    прогоняли те же самые обработчики, что и боевой бот (см. tests/test_handlers_e2e.py)."""
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["access"] = access
    dp["rates"] = rates
    dp["alert_service"] = alert_service
    dp["notifier"] = notifier
    dp["settings"] = settings
    dp["version"] = __version__
    dp["started_at"] = started_at

    state_reset = StateResetMiddleware()
    access_middleware = AccessMiddleware()
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(state_reset)
        observer.outer_middleware(access_middleware)
    dp.include_router(build_router())
    return dp


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

    dp = build_dispatcher(
        db=db, access=access, rates=rate_service, alert_service=alert_service, notifier=notifier,
        settings=settings, started_at=time.time(),
    )

    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    except Exception as exc:  # noqa: BLE001 — список команд не критичен для работы бота
        log.warning("не удалось задать список команд бота: %s", exc)

    log.info("первый опрос источников курсов…")
    await rate_service.poll()
    if rate_service.btc:
        log.info("BTC: %.2f USD (%d/%d источников)", rate_service.btc.price, rate_service.btc.sources_used, rate_service.btc.sources_total)
    else:
        log.warning("после первого опроса цена BTC так и не получена — проверьте интернет (python -m bot --check-apis)")

    # prev_alive читаем ДО запуска фоновых циклов: первый же тик опроса перезапишет эту отметку.
    prev_alive = await get_last_alive(db)
    ctx = SchedulerContext(settings=settings, db=db, rates=rate_service, notifier=notifier, version=__version__)
    tasks = start_background_tasks(ctx)
    # Стартовая сводка — отдельной задачей: она может ждать появления интернета/Telegram (после включения
    # сервера он нередко поднимается позже бота), и это не должно задерживать запуск polling.
    tasks.append(start_startup_report_task(ctx, prev_alive, time.time()))
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
