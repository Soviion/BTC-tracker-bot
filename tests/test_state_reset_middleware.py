"""Юнит-тест StateResetMiddleware без реального Bot/Dispatcher — только FSMContext + фиктивные апдейты."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from bot.keyboards import BTN_MENU
from bot.middlewares import StateResetMiddleware
from bot.states import AdminStates


def make_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


async def call(state: FSMContext, event: Any) -> bool:
    """Возвращает True, если внутренний обработчик был вызван (сработал под текущим состоянием)."""
    called = False

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        nonlocal called
        called = True

    await StateResetMiddleware()(handler, event, {"state": state})
    return called


async def test_free_text_does_not_reset_state() -> None:
    state = make_state()
    await state.set_state(AdminStates.awaiting_user_id)
    msg = Message.model_construct(text="123456789")

    called = await call(state, msg)
    assert called is True
    assert await state.get_state() == AdminStates.awaiting_user_id.state  # диалог не сброшен


async def test_command_resets_state() -> None:
    state = make_state()
    await state.set_state(AdminStates.awaiting_user_id)
    msg = Message.model_construct(text="/menu")

    await call(state, msg)
    assert await state.get_state() is None


async def test_nav_button_text_resets_state() -> None:
    state = make_state()
    await state.set_state(AdminStates.awaiting_user_id)
    msg = Message.model_construct(text=BTN_MENU)

    await call(state, msg)
    assert await state.get_state() is None


async def test_callback_query_resets_state() -> None:
    state = make_state()
    await state.set_state(AdminStates.awaiting_user_id)
    cb = CallbackQuery.model_construct(id="1", data="nav:menu")

    await call(state, cb)
    assert await state.get_state() is None


async def test_no_state_set_is_noop() -> None:
    state = make_state()
    msg = Message.model_construct(text="/menu")
    called = await call(state, msg)
    assert called is True
    assert await state.get_state() is None
