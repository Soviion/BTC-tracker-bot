"""Старт, /id, главное меню, помощь и весь путь «доступа нет» (запрос → решение владельца)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..access import AccessService, RedeemStatus, RequestOutcome, Role, parse_invite_payload
from ..callbacks import Nav, RequestCb
from ..config import Settings
from ..db import Database
from ..notify import Notifier, SendResult
from ..rates.service import RateService
from ..tg_helpers import safe_edit
from ..util import display_name

router = Router(name="common")


async def request_access(message: Message, access: AccessService, notifier: Notifier, owner_ids: frozenset[int]) -> None:
    """Регистрирует запрос доступа и уведомляет владельца. Вызывается и из /start, и из middleware."""
    user = message.from_user
    existing = await access.db.get_request(user.id)
    if existing is not None and existing.status == "denied":
        await message.answer(texts.ACCESS_DENIED)
        return
    outcome = await access.register_request(user.id, username=user.username, full_name=user.full_name or "")
    if outcome is RequestOutcome.NEW:
        await message.answer(texts.ACCESS_REQUEST_SENT)
        text = texts.render_access_request(user.id, user.full_name or "", user.username)
        await notifier.notify_owners(owner_ids, text, keyboards.request_kb(user.id))
    elif outcome is RequestOutcome.PENDING:
        await message.answer(texts.ACCESS_PENDING)
    else:
        await message.answer(texts.ACCESS_DENIED)


async def _send_welcome(message: Message, settings: Settings, rates: RateService, *, is_owner: bool) -> None:
    await message.answer(texts.render_welcome(settings.report_times, settings.tz_name), reply_markup=keyboards.reply_keyboard())
    await message.answer(texts.render_menu(rates.snapshot()), reply_markup=keyboards.main_menu(is_owner))


@router.message(CommandStart())
async def cmd_start(
    message: Message, command: CommandObject, role: Role, is_owner: bool,
    access: AccessService, notifier: Notifier, settings: Settings, rates: RateService,
) -> None:
    user = message.from_user
    if settings.setup_mode:
        await message.answer(texts.render_setup_mode(user.id))
        return

    if role is Role.NONE:
        code = parse_invite_payload(command.args)
        if code:
            result = await access.redeem_invite(code, user.id, username=user.username, full_name=user.full_name or "")
            if result.status is RedeemStatus.OK:
                await _send_welcome(message, settings, rates, is_owner=False)
                joined = texts.render_joined_via_invite(user.id, user.full_name or "", user.username)
                await notifier.notify_owners(settings.owner_ids, joined, keyboards.joined_kb(user.id))
                return
            error = {
                RedeemStatus.USED: texts.INVITE_USED,
                RedeemStatus.EXPIRED: texts.INVITE_EXPIRED,
                RedeemStatus.INVALID: texts.INVITE_INVALID,
            }.get(result.status)
            if error:
                await message.answer(error)
        await request_access(message, access, notifier, settings.owner_ids)
        return

    await _send_welcome(message, settings, rates, is_owner=is_owner)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(texts.render_id(message.from_user.id))


@router.message(Command("menu"))
@router.message(F.text == keyboards.BTN_MENU)
async def cmd_menu(message: Message, rates: RateService, is_owner: bool, db: Database) -> None:
    count = await db.count_alerts(message.from_user.id)
    await message.answer(texts.render_menu(rates.snapshot()), reply_markup=keyboards.main_menu(is_owner, count))


@router.callback_query(Nav.filter(F.to == "menu"))
async def cb_menu(query: CallbackQuery, rates: RateService, is_owner: bool, db: Database) -> None:
    count = await db.count_alerts(query.from_user.id)
    await safe_edit(query, texts.render_menu(rates.snapshot()), keyboards.main_menu(is_owner, count))
    await query.answer()


@router.message(Command("help"))
async def cmd_help(message: Message, is_owner: bool, settings: Settings) -> None:
    text = texts.render_help(is_owner, settings.report_times, settings.tz_name, settings.max_alerts_per_user)
    await message.answer(text, reply_markup=keyboards.back_to_menu())


@router.callback_query(Nav.filter(F.to == "help"))
async def cb_help(query: CallbackQuery, is_owner: bool, settings: Settings) -> None:
    text = texts.render_help(is_owner, settings.report_times, settings.tz_name, settings.max_alerts_per_user)
    await safe_edit(query, text, keyboards.back_to_menu())
    await query.answer()


@router.callback_query(RequestCb.filter())
async def cb_request_decision(
    query: CallbackQuery, callback_data: RequestCb, is_owner: bool,
    access: AccessService, notifier: Notifier, rates: RateService,
) -> None:
    if not is_owner:
        await query.answer(texts.NO_ACCESS_TOAST, show_alert=True)
        return
    uid = callback_data.uid
    if callback_data.act == "ok":
        user, _ = await access.approve_request(uid, by=query.from_user.id)
        name = display_name(user.full_name, user.username, user.user_id)
        await safe_edit(query, f"✅ Доступ выдан: {name}", keyboards.joined_kb(uid))
        sent = await notifier.send(uid, texts.ACCESS_GRANTED, keyboards.reply_keyboard())
        if sent is SendResult.OK:
            await notifier.send(uid, texts.render_menu(rates.snapshot()), keyboards.main_menu(False))
        await query.answer("Доступ выдан")
    else:
        denied = await access.deny_request(uid)
        await safe_edit(query, "❌ Запрос отклонён." if denied else "Запрос уже обработан ранее.")
        if denied:
            await notifier.send(uid, texts.ACCESS_REJECTED_NOTICE)
        await query.answer()
