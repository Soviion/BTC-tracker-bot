"""Настройка того, в какие слоты присылать ежедневную сводку."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import keyboards, texts
from ..callbacks import Nav, ReportCb
from ..config import Settings
from ..db import Database
from ..tg_helpers import safe_edit

router = Router(name="reports")


def _slot_from_key(settings: Settings, key: str) -> str | None:
    for slot in settings.report_times:
        if slot.strftime("%H%M") == key:
            return slot.strftime("%H:%M")
    return None


async def _render(user_id: int, db: Database, settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    user = await db.get_user(user_id)
    off = user.reports_off if user else frozenset()
    return texts.render_reports_settings(settings.report_times, off, settings.tz_name), keyboards.reports_kb(settings.report_times, off)


@router.message(Command("reports"))
async def cmd_reports(message: Message, db: Database, settings: Settings) -> None:
    text, kb = await _render(message.from_user.id, db, settings)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Nav.filter(F.to == "reports"))
async def cb_reports(query: CallbackQuery, db: Database, settings: Settings) -> None:
    text, kb = await _render(query.from_user.id, db, settings)
    await safe_edit(query, text, kb)
    await query.answer()


@router.callback_query(ReportCb.filter())
async def cb_toggle(query: CallbackQuery, callback_data: ReportCb, db: Database, settings: Settings) -> None:
    user = await db.get_user(query.from_user.id)
    off = set(user.reports_off) if user else set()
    all_slots = {slot.strftime("%H:%M") for slot in settings.report_times}

    if callback_data.act == "on":
        off.clear()
    elif callback_data.act == "off":
        off = set(all_slots)
    else:
        label = _slot_from_key(settings, callback_data.slot)
        if label:
            off.symmetric_difference_update({label})

    await db.set_reports_off(query.from_user.id, off)
    text, kb = await _render(query.from_user.id, db, settings)
    await safe_edit(query, text, kb)
    await query.answer()
