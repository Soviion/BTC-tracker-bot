"""Состояния FSM (aiogram) для коротких диалогов вида «жду от вас одно сообщение»."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    awaiting_user_id = State()  # владелец нажал «Добавить по ID» — ждём Telegram ID следующим сообщением
