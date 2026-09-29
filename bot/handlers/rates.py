"""Экран «Курсы» — по команде, кнопке снизу и через inline-навигацию (с обновлением по кнопке)."""

from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..callbacks import Nav
from ..config import Settings
from ..db import Database
from ..history import load_stats
from ..rates.service import RateService
from ..tg_helpers import safe_edit

router = Router(name="rates")


async def _render(rates: RateService, db: Database, settings: Settings) -> str:
    snap = await rates.refresh_if_stale()
    now = time.time()
    stats = await load_stats(db, now, snap.btc.price) if snap.btc else None
    return texts.render_rates(snap, stats, now=now, tz=settings.tz, stale_after=settings.stale_after)


@router.message(Command("rates"))
@router.message(F.text == keyboards.BTN_RATES)
async def show_rates(message: Message, rates: RateService, db: Database, settings: Settings) -> None:
    await message.answer(await _render(rates, db, settings), reply_markup=keyboards.rates_kb())


@router.callback_query(Nav.filter(F.to == "rates"))
async def cb_rates(query: CallbackQuery, rates: RateService, db: Database, settings: Settings) -> None:
    await safe_edit(query, await _render(rates, db, settings), keyboards.rates_kb())
    await query.answer()
