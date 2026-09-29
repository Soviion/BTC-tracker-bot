"""Мелкие помощники для обработчиков aiogram."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup

log = logging.getLogger(__name__)


async def safe_edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение под кнопкой. Не падает на «not modified» / недоступном сообщении —
    это обычная ситуация при двойных нажатиях и сообщениях-«призраках» после перезапуска бота."""
    message = query.message
    if message is None or isinstance(message, InaccessibleMessage):
        return
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        log.debug("не удалось отредактировать сообщение %s: %s", message.message_id, exc)
