"""Отправка сообщений в Telegram с разбором типовых ошибок (блокировка бота, лимиты, сетевые сбои)."""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from .db import Database

log = logging.getLogger(__name__)

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup


class SendResult(enum.Enum):
    OK = "ok"
    RETRY = "retry"  # временный сбой (сеть, лимиты) — стоит попробовать позже
    FAILED = "failed"  # окончательно: пользователь заблокировал бота, чат не найден и т.п.


@dataclass(frozen=True)
class BroadcastStats:
    sent: int
    retry: int
    failed: int


class Notifier:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        *,
        concurrency: int = 8,
        attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._db = db
        self._attempts = attempts
        self._sleep = sleep
        self._sem = asyncio.Semaphore(concurrency)

    async def send(
        self, user_id: int, text: str, reply_markup: Markup | None = None, *, silent: bool = False
    ) -> SendResult:
        for attempt in range(1, self._attempts + 1):
            last = attempt == self._attempts
            try:
                await self._bot.send_message(
                    user_id, text, reply_markup=reply_markup, disable_notification=silent or None
                )
                return SendResult.OK
            except TelegramRetryAfter as exc:
                log.warning("Telegram просит подождать %s с (отправка %s)", exc.retry_after, user_id)
                if last:
                    return SendResult.RETRY
                await self._sleep(min(float(exc.retry_after) + 0.5, 30.0))
            except TelegramForbiddenError:
                await self._db.set_bot_blocked(user_id, True)
                log.info("пользователь %s заблокировал бота — сводки ему приостановлены", user_id)
                return SendResult.FAILED
            except TelegramUnauthorizedError:
                log.critical("Telegram отклонил токен бота (401) — проверьте BOT_TOKEN")
                return SendResult.FAILED
            except TelegramBadRequest as exc:
                log.warning("не удалось отправить сообщение %s: %s", user_id, exc)
                return SendResult.FAILED
            except (TelegramNetworkError, TelegramServerError) as exc:
                log.warning("сбой связи с Telegram при отправке %s (попытка %d/%d): %s", user_id, attempt, self._attempts, exc)
                if last:
                    return SendResult.RETRY
                await self._sleep(2.0 * attempt)
            except TelegramAPIError as exc:
                log.warning("ошибка Telegram API при отправке %s: %s", user_id, exc)
                return SendResult.FAILED
            except Exception:  # noqa: BLE001
                log.exception("неожиданная ошибка при отправке сообщения %s", user_id)
                return SendResult.FAILED
        return SendResult.RETRY

    async def broadcast(
        self, user_ids: Iterable[int], text: str, reply_markup: Markup | None = None, *, silent: bool = False
    ) -> BroadcastStats:
        async def one(uid: int) -> SendResult:
            async with self._sem:
                result = await self.send(uid, text, reply_markup, silent=silent)
                await self._sleep(0.04)  # ~25 сообщений/с — в пределах лимитов Telegram
                return result

        results = await asyncio.gather(*(one(uid) for uid in user_ids))
        return BroadcastStats(
            sent=results.count(SendResult.OK),
            retry=results.count(SendResult.RETRY),
            failed=results.count(SendResult.FAILED),
        )

    async def notify_owners(
        self, owner_ids: Iterable[int], text: str, reply_markup: Markup | None = None
    ) -> BroadcastStats:
        return await self.broadcast(owner_ids, text, reply_markup)
