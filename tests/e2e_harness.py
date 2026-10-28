"""Стенд для сквозных тестов: настоящий диспетчер aiogram, middleware, обработчики, сервисы и БД —
подменена только сеть Telegram (RecordingSession запоминает вызовы Bot API и отвечает заготовками).

Каждое исходящее сообщение и клавиатура проверяются на то, что Telegram отвергнул бы в бою:
неэкранированный HTML / неподдерживаемые теги, текст длиннее 4096, callback_data длиннее 64 байт."""

from __future__ import annotations

import itertools
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage
from aiogram.types import Chat, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, Update, User

from bot import __version__
from bot.access import AccessService
from bot.alerts import AlertService
from bot.config import Settings, load_settings
from bot.db import Database
from bot.main import build_dispatcher
from bot.notify import Notifier
from bot.rates.service import RateService
from bot.scheduler import SchedulerContext
from tests.samples import FakeFetcher

TOKEN = "123456789:AAEfakeTokenForE2ETests1234567890abc"
BOT_USERNAME = "btc_tracker_test_bot"

_ids = itertools.count(1)


# --------------------------------------------------------------------------- #
# Проверки «примет ли это Telegram»
# --------------------------------------------------------------------------- #
_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a",
                 "tg-spoiler", "blockquote", "tg-emoji"}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^>]*)>")
_ENTITY_RE = re.compile(r"&(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);")


def _check_plain(chunk: str, whole: str) -> None:
    assert "<" not in chunk and ">" not in chunk, f"неэкранированный < или > в тексте: {whole!r}"
    for m in re.finditer("&", chunk):
        assert _ENTITY_RE.match(chunk, m.start()), f"неэкранированный & в тексте: {whole!r}"


def assert_valid_telegram_html(text: str) -> None:
    assert 0 < len(text) <= 4096, f"длина сообщения {len(text)} вне 1..4096"
    stack: list[str] = []
    pos = 0
    for m in _TAG_RE.finditer(text):
        _check_plain(text[pos:m.start()], text)
        closing, name = m.group(1), m.group(2).lower()
        assert name in _ALLOWED_TAGS, f"тег <{name}> не поддерживается Telegram: {text!r}"
        if closing:
            assert stack and stack[-1] == name, f"нарушена вложенность </{name}>: {text!r}"
            stack.pop()
        else:
            stack.append(name)
        pos = m.end()
    _check_plain(text[pos:], text)
    assert not stack, f"не закрыты теги {stack}: {text!r}"


def assert_valid_markup(markup: Any) -> None:
    if isinstance(markup, InlineKeyboardMarkup):
        total = 0
        for row in markup.inline_keyboard:
            assert 1 <= len(row) <= 8, "в ряду inline-кнопок должно быть 1..8 кнопок"
            for button in row:
                total += 1
                assert button.text.strip(), "пустой текст кнопки"
                assert (button.callback_data is None) != (button.url is None), "у кнопки ровно одно из: callback_data/url"
                if button.callback_data is not None:
                    assert len(button.callback_data.encode()) <= 64, f"callback_data > 64 байт: {button.callback_data!r}"
                if button.url is not None:
                    assert button.url.startswith(("https://", "http://", "tg://")), button.url
        assert total <= 100, "в inline-клавиатуре не больше 100 кнопок"
    elif isinstance(markup, ReplyKeyboardMarkup):
        assert markup.keyboard and all(row for row in markup.keyboard)


# --------------------------------------------------------------------------- #
# Подменённая сеть Telegram
# --------------------------------------------------------------------------- #
@dataclass
class Sent:
    """Одно исходящее действие бота: send (новое сообщение), edit (правка), toast (ответ на нажатие кнопки)."""

    kind: str
    chat_id: int | None
    text: str
    markup: Any = None
    show_alert: bool = False


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []
        self._message_ids = itertools.count(10_000)

    async def close(self) -> None:
        return None

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        raise NotImplementedError
        yield b""  # pragma: no cover — делает метод асинхронным генератором, как требует BaseSession

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=424242, is_bot=True, first_name="Tracker", username=BOT_USERNAME)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(
                message_id=next(self._message_ids), date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id or 0, type="private"), text=method.text,
            )
        return True


def to_sent(method: Any) -> Sent | None:
    if isinstance(method, SendMessage):
        return Sent("send", method.chat_id, method.text, method.reply_markup)
    if isinstance(method, EditMessageText):
        return Sent("edit", method.chat_id, method.text, method.reply_markup)
    if isinstance(method, AnswerCallbackQuery):
        return Sent("toast", None, method.text or "", None, bool(method.show_alert))
    return None


# --------------------------------------------------------------------------- #
# Клавиатуры: найти кнопку и «нажать» её
# --------------------------------------------------------------------------- #
def buttons(markup: Any) -> list[tuple[str, str | None, str | None]]:
    """[(текст, callback_data, url)] всех inline-кнопок."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [(b.text, b.callback_data, b.url) for row in markup.inline_keyboard for b in row]


def press(markup: Any, label_part: str) -> str:
    for label, data, _url in buttons(markup):
        if label_part in label and data is not None:
            return data
    raise AssertionError(f"нет кнопки с {label_part!r}; есть: {[label for label, _, _ in buttons(markup)]}")


def url_of(markup: Any, label_part: str) -> str:
    for label, _data, url in buttons(markup):
        if label_part in label and url is not None:
            return url
    raise AssertionError(f"нет URL-кнопки с {label_part!r}")


# --------------------------------------------------------------------------- #
# Входящие апдейты
# --------------------------------------------------------------------------- #
def _user(uid: int, name: str, username: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": uid, "is_bot": False, "first_name": name}
    if username:
        payload["username"] = username
    return payload


def _message(uid: int, text: str | None, name: str, username: str | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": next(_ids), "date": 1_790_000_000, "chat": {"id": uid, "type": "private"},
        "from": _user(uid, name, username),
    }
    if text is not None:
        payload["text"] = text
        if text.startswith("/"):
            payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    payload.update(extra or {})
    return payload


class App:
    def __init__(self, *, bot: Bot, dp: Dispatcher, session: RecordingSession, db: Database, rates: RateService,
                 fetcher: FakeFetcher, settings: Settings, ctx: SchedulerContext) -> None:
        self.bot, self.dp, self.session, self.db = bot, dp, session, db
        self.rates, self.fetcher, self.settings, self.ctx = rates, fetcher, settings, ctx

    def since(self, mark: int) -> list[Sent]:
        out = []
        for method in self.session.calls[mark:]:
            item = to_sent(method)
            if item is None:
                continue
            if item.kind in ("send", "edit"):
                assert_valid_telegram_html(item.text)
                assert_valid_markup(item.markup)
            out.append(item)
        return out

    @property
    def mark(self) -> int:
        return len(self.session.calls)

    async def _feed(self, payload: dict[str, Any]) -> list[Sent]:
        mark = self.mark
        update = Update.model_validate({"update_id": next(_ids), **payload}, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)
        return self.since(mark)

    async def say(self, uid: int, text: str | None, *, name: str = "User", username: str | None = None,
                  extra: dict[str, Any] | None = None) -> list[Sent]:
        return await self._feed({"message": _message(uid, text, name, username, extra)})

    async def tap(self, uid: int, data: str, *, name: str = "User", username: str | None = None) -> list[Sent]:
        callback = {
            "id": str(next(_ids)), "from": _user(uid, name, username), "chat_instance": "ci", "data": data,
            "message": _message(uid, "(старое сообщение)", name, username),
        }
        return await self._feed({"callback_query": callback})


async def _no_sleep(_seconds: float) -> None:
    return None


async def make_app(db: Database, *, owner_ids: str = "1000", price: float = 83000.0) -> App:
    session = RecordingSession()
    bot = Bot(token=TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    settings = load_settings({"BOT_TOKEN": TOKEN, "OWNER_ID": owner_ids, "DATABASE_URL": "postgresql://x:x@localhost/x"})
    fetcher = FakeFetcher(price)
    rates = RateService(fetcher)
    await rates.poll()
    await db.sync_owners(settings.owner_ids)
    notifier = Notifier(bot, db, sleep=_no_sleep)
    access = AccessService(db, settings.owner_ids, invite_ttl_seconds=settings.invite_ttl_hours * 3600)
    alerts = AlertService(db, max_per_user=settings.max_alerts_per_user)
    dp = build_dispatcher(db=db, access=access, rates=rates, alert_service=alerts, notifier=notifier,
                          settings=settings, started_at=time.time())
    ctx = SchedulerContext(settings=settings, db=db, rates=rates, notifier=notifier, version=__version__)
    return App(bot=bot, dp=dp, session=session, db=db, rates=rates, fetcher=fetcher, settings=settings, ctx=ctx)
