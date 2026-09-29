"""Middleware'ы диспетчера: проверка доступа и сброс «зависших» состояний FSM."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from . import keyboards, texts
from .access import AccessService, Role
from .handlers.common import request_access

log = logging.getLogger(__name__)

_BYPASS_COMMANDS = {"/start", "/id"}
_NAV_BUTTON_TEXTS = {keyboards.BTN_RATES, keyboards.BTN_ALERTS, keyboards.BTN_MENU}


class AccessMiddleware(BaseMiddleware):
    """Единая точка проверки доступа: остальным обработчикам не нужно знать про Role вообще (но они могут
    принять `role`/`is_owner` как параметр — эта middleware кладёт их в данные апдейта)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:  # служебные апдейты без пользователя (poll-ответы и т.п.) — пропускаем как есть
            return await handler(event, data)

        access: AccessService = data["access"]
        role = await access.role_of(user.id)
        data["role"] = role
        data["is_owner"] = role is Role.OWNER

        if role is not Role.NONE:
            try:
                await access.db.touch_user(user.id, user.username, user.full_name or "", int(time.time()))
            except Exception:  # noqa: BLE001 — обновление «был в сети» не должно блокировать саму команду
                log.warning("не удалось обновить профиль пользователя %s", user.id, exc_info=True)
            return await handler(event, data)

        if isinstance(event, Message):
            first_word = (event.text or "").split()[0].split("@")[0].lower() if event.text else ""
            if first_word in _BYPASS_COMMANDS:
                return await handler(event, data)
            await request_access(event, access, data["notifier"], data["settings"].owner_ids)
            return None

        if isinstance(event, CallbackQuery):
            await event.answer(texts.NO_ACCESS_TOAST, show_alert=True)
            return None

        return None  # прочие типы апдейтов (например, my_chat_member) от чужих просто игнорируем


class StateResetMiddleware(BaseMiddleware):
    """Сбрасывает точечные состояния FSM (например, «жду Telegram ID для /adduser») при явном переходе
    в другое место интерфейса — команда, кнопка меню или любой inline-callback. Иначе следующее
    сообщение (скажем, число для алерта) могло бы по ошибке попасть в незавершённый старый диалог.
    Свободный текст не трогаем — его и должен обработать активный диалог."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        if state is not None and await state.get_state() is not None:
            is_navigation = isinstance(event, CallbackQuery) or (
                isinstance(event, Message) and bool(event.text)
                and (event.text.startswith("/") or event.text in _NAV_BUTTON_TEXTS)
            )
            if is_navigation:
                await state.clear()
        return await handler(event, data)
