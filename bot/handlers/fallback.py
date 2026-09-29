"""Ловит любой текст/апдейт, не подошедший другим обработчикам. Должен подключаться последним."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from .. import keyboards, texts

router = Router(name="fallback")


@router.message(StateFilter(None))
async def fallback_any(message: Message) -> None:
    """Сюда доходит и обычный текст, который не разобрали как цену, и стикеры/фото/голос и т.п."""
    await message.answer(texts.HINT_UNKNOWN, reply_markup=keyboards.back_to_menu())
