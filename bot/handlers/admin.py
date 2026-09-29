"""Панель владельца: пользователи, приглашения, добавление/удаление по ID, статус источников."""

from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import keyboards, texts
from ..access import AccessService
from ..callbacks import AdminCb, Nav
from ..config import Settings
from ..db import BIGINT_MAX, Database
from ..notify import Notifier, SendResult
from ..rates.service import RateService
from ..states import AdminStates
from ..tg_helpers import safe_edit
from ..util import display_name

router = Router(name="admin")


async def _invite_link(bot: Bot, code: str) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=inv_{code}"


async def _admin_home(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    users = await db.list_users()
    invites = await db.count_open_invites()
    alerts = await db.count_alerts()
    return texts.render_admin(len(users), invites, alerts), keyboards.admin_kb(len(users), invites)


@router.callback_query(Nav.filter(F.to == "admin"))
async def cb_admin_home(query: CallbackQuery, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    text, kb = await _admin_home(db)
    await safe_edit(query, text, kb)
    await query.answer()


# --------------------------------------------------------------------------- #
# Пользователи
# --------------------------------------------------------------------------- #
@router.message(Command("users"))
async def cmd_users(message: Message, is_owner: bool, db: Database, settings: Settings) -> None:
    if not is_owner:
        return
    users = await db.list_users()
    await message.answer(texts.render_users(users, settings.owner_ids, settings.tz), reply_markup=keyboards.users_kb(users, settings.owner_ids))


@router.callback_query(AdminCb.filter(F.act == "users"))
async def cb_users(query: CallbackQuery, is_owner: bool, db: Database, settings: Settings) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    users = await db.list_users()
    await safe_edit(query, texts.render_users(users, settings.owner_ids, settings.tz), keyboards.users_kb(users, settings.owner_ids))
    await query.answer()


@router.callback_query(AdminCb.filter(F.act == "rm"))
async def cb_remove_confirm(query: CallbackQuery, callback_data: AdminCb, is_owner: bool, db: Database) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    user = await db.get_user(callback_data.uid)
    name = display_name(user.full_name, user.username, user.user_id) if user else f"id {callback_data.uid}"
    kb = keyboards.confirm_kb(AdminCb(act="rmyes", uid=callback_data.uid).pack(), Nav(to="admin").pack())
    await safe_edit(query, f"❌ Убрать доступ у {name}?", kb)
    await query.answer()


@router.callback_query(AdminCb.filter(F.act == "rmyes"))
async def cb_remove(query: CallbackQuery, callback_data: AdminCb, is_owner: bool, access: AccessService, notifier: Notifier) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    removed = await access.revoke(callback_data.uid)
    head = "✅ Доступ удалён.\n\n" if removed else "Не найдено — возможно, уже удалён.\n\n"
    text, kb = await _admin_home(access.db)
    await safe_edit(query, head + text, kb)
    await query.answer()
    if removed:
        await notifier.send(callback_data.uid, "⛔ Ваш доступ к боту отозван владельцем.")


# --------------------------------------------------------------------------- #
# Приглашения
# --------------------------------------------------------------------------- #
@router.message(Command("invite"))
async def cmd_invite(message: Message, is_owner: bool, access: AccessService, settings: Settings) -> None:
    if not is_owner:
        return
    invite = await access.create_invite(message.from_user.id)
    link = await _invite_link(message.bot, invite.code)
    await message.answer(texts.render_invite(link, settings.invite_ttl_hours), reply_markup=keyboards.invite_kb(link))


@router.callback_query(AdminCb.filter(F.act == "invite"))
async def cb_invite(query: CallbackQuery, is_owner: bool, access: AccessService, settings: Settings) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    invite = await access.create_invite(query.from_user.id)
    link = await _invite_link(query.bot, invite.code)
    await safe_edit(query, texts.render_invite(link, settings.invite_ttl_hours), keyboards.invite_kb(link))
    await query.answer("Ссылка создана")


@router.callback_query(AdminCb.filter(F.act == "revoke"))
async def cb_revoke(query: CallbackQuery, is_owner: bool, access: AccessService, settings: Settings) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    n = await access.db.revoke_open_invites()
    invite = await access.create_invite(query.from_user.id)
    link = await _invite_link(query.bot, invite.code)
    text = f"🗑 Отозвано ссылок: {n}.\n\n" + texts.render_invite(link, settings.invite_ttl_hours)
    await safe_edit(query, text, keyboards.invite_kb(link))
    await query.answer()


# --------------------------------------------------------------------------- #
# Добавление по ID
# --------------------------------------------------------------------------- #
def _parse_telegram_id(raw: str) -> int | None:
    raw = raw.strip()
    if not raw.isdigit():
        return None
    uid = int(raw)
    return uid if 0 < uid <= BIGINT_MAX else None


async def _add_by_text(message: Message, raw: str, access: AccessService, notifier: Notifier, rates: RateService) -> None:
    uid = _parse_telegram_id(raw)
    if uid is None:
        await message.answer("🤔 Нужен числовой Telegram ID, например 123456789.", reply_markup=keyboards.admin_back_kb())
        return
    user, created = await access.grant(uid, added_by=message.from_user.id)
    if not created:
        await message.answer(f"ℹ️ У <code>{uid}</code> уже есть доступ.", reply_markup=keyboards.admin_back_kb())
        return
    await message.answer(f"✅ Доступ выдан: <code>{uid}</code>", reply_markup=keyboards.admin_back_kb())
    sent = await notifier.send(uid, texts.ACCESS_GRANTED, keyboards.reply_keyboard())
    if sent is SendResult.OK:
        await notifier.send(uid, texts.render_menu(rates.snapshot()), keyboards.main_menu(False))


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject, is_owner: bool, access: AccessService, notifier: Notifier, rates: RateService) -> None:
    if not is_owner:
        return
    await _add_by_text(message, command.args or "", access, notifier, rates)


@router.message(Command("removeuser"))
async def cmd_removeuser(message: Message, command: CommandObject, is_owner: bool, access: AccessService, notifier: Notifier) -> None:
    if not is_owner:
        return
    uid = _parse_telegram_id(command.args or "")
    if uid is None:
        await message.answer("🤔 Нужен числовой Telegram ID, например 123456789.")
        return
    removed = await access.revoke(uid)
    await message.answer("✅ Доступ удалён." if removed else "Не найдено.")
    if removed:
        await notifier.send(uid, "⛔ Ваш доступ к боту отозван владельцем.")


@router.callback_query(AdminCb.filter(F.act == "add"))
async def cb_add_prompt(query: CallbackQuery, is_owner: bool, state: FSMContext) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    await state.set_state(AdminStates.awaiting_user_id)
    await safe_edit(query, texts.render_add_user_prompt(), keyboards.admin_back_kb())
    await query.answer()


@router.message(StateFilter(AdminStates.awaiting_user_id))
async def msg_add_id(message: Message, is_owner: bool, access: AccessService, notifier: Notifier, rates: RateService, state: FSMContext) -> None:
    await state.clear()
    if not is_owner:  # доступ мог быть отозван, пока ждали ввод
        return
    await _add_by_text(message, message.text or "", access, notifier, rates)


# --------------------------------------------------------------------------- #
# Статус источников
# --------------------------------------------------------------------------- #
def _status_text(db_users: int, db_alerts: int, rates: RateService, version: str, started_at: float) -> str:
    now = time.time()
    return texts.render_status(
        rates.source_overview(), rates.snapshot(), now=now, started_at=started_at,
        users=db_users, alerts=db_alerts, version=version,
    )


@router.message(Command("status"))
async def cmd_status(message: Message, is_owner: bool, db: Database, rates: RateService, version: str, started_at: float) -> None:
    if not is_owner:
        return
    text = _status_text(await db.count_users(), await db.count_alerts(), rates, version, started_at)
    await message.answer(text, reply_markup=keyboards.admin_back_kb())


@router.callback_query(AdminCb.filter(F.act == "status"))
async def cb_status(query: CallbackQuery, is_owner: bool, db: Database, rates: RateService, version: str, started_at: float) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    text = _status_text(await db.count_users(), await db.count_alerts(), rates, version, started_at)
    await safe_edit(query, text, keyboards.admin_back_kb())
    await query.answer()
