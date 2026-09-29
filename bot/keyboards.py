"""Клавиатуры: постоянная (внизу чата) и inline-кнопки под сообщениями."""

from __future__ import annotations

from datetime import time
from typing import Iterable, Sequence
from urllib.parse import quote

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .callbacks import AdminCb, AlertCb, Nav, ReportCb, RequestCb
from .db import Alert, User
from .texts import alert_label

BTN_RATES = "💱 Курсы"
BTN_ALERTS = "🔔 Алерты"
BTN_MENU = "☰ Меню"

PCT_STEPS = (-10, -5, -1, 1, 5, 10)


def slot_key(slot: time | str) -> str:
    """08:00 → "0800" (для callback_data)."""
    label = slot if isinstance(slot, str) else slot.strftime("%H:%M")
    return label.replace(":", "")


def slot_label(slot: time) -> str:
    return slot.strftime("%H:%M")


def reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_RATES), KeyboardButton(text=BTN_ALERTS), KeyboardButton(text=BTN_MENU)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Цена для алерта, например 90000",
    )


def _nav(text: str, to: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=Nav(to=to).pack())


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_nav("⬅️ Меню", "menu")]])


def main_menu(is_owner: bool, alert_count: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_nav("💱 Курсы", "rates"), _nav(f"🔔 Мои алерты{f' ({alert_count})' if alert_count else ''}", "alerts"))
    b.row(
        InlineKeyboardButton(text="➕ Новый алерт", callback_data=AlertCb(act="new").pack()),
        _nav("⏰ Сводки", "reports"),
    )
    b.row(_nav("ℹ️ Помощь", "help"), *([_nav("👑 Админ", "admin")] if is_owner else []))
    return b.as_markup()


def rates_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_nav("🔄 Обновить", "rates"))
    b.row(_nav("🔔 Алерты", "alerts"), _nav("⬅️ Меню", "menu"))
    return b.as_markup()


# --------------------------------------------------------------------------- #
# Алерты
# --------------------------------------------------------------------------- #
def alerts_list_kb(alerts: Sequence[Alert], price: float | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for a in alerts:
        b.row(InlineKeyboardButton(text=alert_label(a, price), callback_data=AlertCb(act="open", id=a.id).pack()))
    b.row(InlineKeyboardButton(text="➕ Новый алерт", callback_data=AlertCb(act="new").pack()))
    if len(alerts) > 1:
        b.row(InlineKeyboardButton(text="🧹 Удалить все", callback_data=AlertCb(act="clear").pack()))
    b.row(_nav("⬅️ Меню", "menu"))
    return b.as_markup()


def alert_detail_kb(alert: Alert) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not alert.active:
        b.row(InlineKeyboardButton(text="🔁 Включить снова (повтор)", callback_data=AlertCb(act="rep", id=alert.id).pack()))
    else:
        text = "🔁 Повторять: включено" if alert.repeat else "🔁 Повторять: выключено"
        b.row(InlineKeyboardButton(text=text, callback_data=AlertCb(act="rep", id=alert.id).pack()))
    b.row(
        InlineKeyboardButton(text="🗑 Удалить", callback_data=AlertCb(act="del", id=alert.id).pack()),
        _nav("⬅️ К алертам", "alerts"),
    )
    return b.as_markup()


def alert_created_kb(alert: Alert) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    text = "🔁 Повторять: включено" if alert.repeat else "🔁 Повторять при каждом пересечении"
    b.row(InlineKeyboardButton(text=text, callback_data=AlertCb(act="rep", id=alert.id).pack()))
    b.row(
        InlineKeyboardButton(text="🗑 Удалить", callback_data=AlertCb(act="delyes", id=alert.id).pack()),
        _nav("🔔 Мои алерты", "alerts"),
    )
    return b.as_markup()


def alert_fired_kb(alert: Alert) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not alert.repeat:
        b.row(InlineKeyboardButton(text="🔁 Сообщать снова", callback_data=AlertCb(act="rep", id=alert.id).pack()))
    b.row(
        InlineKeyboardButton(text="➕ Новый алерт", callback_data=AlertCb(act="new").pack()),
        _nav("🔔 Алерты", "alerts"),
    )
    return b.as_markup()


def new_alert_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for step in PCT_STEPS:
        sign = "+" if step > 0 else "−"
        icon = "📈" if step > 0 else "📉"
        b.button(text=f"{icon} {sign}{abs(step)}%", callback_data=AlertCb(act="pct", val=step).pack())
    b.adjust(3, 3)
    b.row(_nav("❌ Отмена", "alerts"))
    return b.as_markup()


def confirm_kb(yes: str, no: str, yes_text: str = "✅ Да", no_text: str = "⬅️ Отмена") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=yes_text, callback_data=yes),
            InlineKeyboardButton(text=no_text, callback_data=no),
        ]]
    )


# --------------------------------------------------------------------------- #
# Сводки
# --------------------------------------------------------------------------- #
def reports_kb(slots: Iterable[time], off: frozenset[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    slots = list(slots)
    for slot in slots:
        mark = "❌" if slot_label(slot) in off else "✅"
        b.button(text=f"{mark} {slot_label(slot)}", callback_data=ReportCb(act="t", slot=slot_key(slot)).pack())
    b.adjust(2)
    all_off = all(slot_label(s) in off for s in slots)
    if all_off:
        b.row(InlineKeyboardButton(text="🔔 Включить все", callback_data=ReportCb(act="on").pack()))
    else:
        b.row(InlineKeyboardButton(text="🔕 Выключить все", callback_data=ReportCb(act="off").pack()))
    b.row(_nav("⬅️ Меню", "menu"))
    return b.as_markup()


# --------------------------------------------------------------------------- #
# Админка
# --------------------------------------------------------------------------- #
def admin_kb(users_count: int, open_invites: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=f"👥 Пользователи ({users_count})", callback_data=AdminCb(act="users").pack()),
        InlineKeyboardButton(text="➕ Пригласить", callback_data=AdminCb(act="invite").pack()),
    )
    b.row(
        InlineKeyboardButton(text="🆔 Добавить по ID", callback_data=AdminCb(act="add").pack()),
        InlineKeyboardButton(text="📡 Статус", callback_data=AdminCb(act="status").pack()),
    )
    b.row(_nav("⬅️ Меню", "menu"))
    return b.as_markup()


def users_kb(users: Sequence[User], owner_ids: frozenset[int], *, limit: int = 40) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    removable = [u for u in users if u.user_id not in owner_ids][:limit]
    for u in removable:
        # в тексте кнопки HTML не работает — берём простое читаемое имя
        label = (u.full_name.strip() or (f"@{u.username}" if u.username else f"id {u.user_id}"))[:28]
        b.row(InlineKeyboardButton(text=f"❌ {label}", callback_data=AdminCb(act="rm", uid=u.user_id).pack()))
    b.row(
        InlineKeyboardButton(text="➕ Пригласить", callback_data=AdminCb(act="invite").pack()),
        InlineKeyboardButton(text="🆔 По ID", callback_data=AdminCb(act="add").pack()),
    )
    b.row(_nav("⬅️ Админ", "admin"))
    return b.as_markup()


def invite_kb(link: str) -> InlineKeyboardMarkup:
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('Доступ к боту с курсами BTC и BYN:', safe='')}"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📤 Отправить ссылку", url=share))
    b.row(
        InlineKeyboardButton(text="🔄 Новая ссылка", callback_data=AdminCb(act="invite").pack()),
        InlineKeyboardButton(text="🗑 Отозвать все", callback_data=AdminCb(act="revoke").pack()),
    )
    b.row(_nav("⬅️ Админ", "admin"))
    return b.as_markup()


def request_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data=RequestCb(act="ok", uid=uid).pack()),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=RequestCb(act="no", uid=uid).pack()),
        ]]
    )


def joined_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Удалить доступ", callback_data=AdminCb(act="rm", uid=uid).pack())]]
    )


def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_nav("⬅️ Админ", "admin")]])
