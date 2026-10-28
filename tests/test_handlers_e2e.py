"""Сквозные тесты обработчиков: настоящий диспетчер + middleware + сервисы + БД (сеть Telegram подменена).

Сценарии идут от лица пользователей с разными ролями; кнопки «нажимаются» по реальным клавиатурам,
которые отдал бот, — так проверяется и связка «клавиатура → callback_data → обработчик»."""

from __future__ import annotations

import re
from collections import deque

import pytest_asyncio
from aiogram.types import ReplyKeyboardMarkup

from bot.callbacks import AdminCb, AlertCb, Nav
from bot.db import Database
from tests.e2e_harness import App, Sent, buttons, make_app, press, url_of

OWNER, FRIEND, STRANGER, STRANGER2 = 1000, 2000, 3000, 4000


@pytest_asyncio.fixture
async def app(db: Database) -> App:
    return await make_app(db)


def chat(sent: list[Sent], uid: int, kinds: tuple[str, ...] = ("send", "edit")) -> list[Sent]:
    return [s for s in sent if s.chat_id == uid and s.kind in kinds]


async def test_owner_start_shows_welcome_and_menu(app: App) -> None:
    sent = await app.say(OWNER, "/start", name="Boss", username="boss")

    messages = chat(sent, OWNER, ("send",))
    assert len(messages) == 2
    assert "BTC Tracker" in messages[0].text
    assert isinstance(messages[0].markup, ReplyKeyboardMarkup)  # нижняя постоянная клавиатура
    assert "Выберите действие" in messages[1].text and "BYN" in messages[1].text
    assert any("Админ" in label for label, _, _ in buttons(messages[1].markup))  # у владельца есть админка
    assert (await app.db.get_user(OWNER)).username == "boss"  # профиль обновился middleware'ом
