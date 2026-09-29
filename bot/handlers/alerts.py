"""Создание и управление алертами: команды, кнопки процентов и обычное число в чате."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import keyboards, texts
from ..alerts import MAX_TARGET_RATIO, MIN_TARGET_RATIO, AlertService, CreateResult, CreateStatus
from ..callbacks import AlertCb, Nav
from ..config import Settings
from ..db import Database
from ..rates.service import RateService
from ..tg_helpers import safe_edit
from ..util import parse_price

router = Router(name="alerts")


def _price(rates: RateService) -> float | None:
    quote = rates.fresh_btc()
    return quote.price if quote else None


def _is_price_text(message: Message) -> bool:
    return bool(message.text) and parse_price(message.text) is not None


def _render_result(result: CreateResult, target: float, price: float | None, settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    if result.status is CreateStatus.NO_PRICE:
        return texts.ALERT_ERRORS["no_price"], keyboards.back_to_menu()
    if result.status is CreateStatus.EQUALS_CURRENT:
        return texts.ALERT_ERRORS["equals_current"], keyboards.back_to_menu()
    if result.status is CreateStatus.OUT_OF_RANGE:
        assert price is not None
        return texts.render_alert_out_of_range(target, price, price * MIN_TARGET_RATIO, price * MAX_TARGET_RATIO), keyboards.back_to_menu()
    if result.status is CreateStatus.LIMIT:
        return texts.render_alert_limit(settings.max_alerts_per_user), keyboards.back_to_menu()
    assert result.alert is not None and price is not None
    return texts.render_alert_created(result.alert, price, duplicate=result.status is CreateStatus.DUPLICATE), keyboards.alert_created_kb(result.alert)


async def _create(user_id: int, target: float, alert_service: AlertService, rates: RateService, settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    price = _price(rates)
    result = await alert_service.create(user_id, target, price)
    return _render_result(result, target, price, settings)


@router.message(Command("alert"))
async def cmd_alert(message: Message, command: CommandObject, alert_service: AlertService, rates: RateService, settings: Settings) -> None:
    target = parse_price(command.args or "")
    if target is None:
        await message.answer(texts.render_new_alert_prompt(_price(rates)), reply_markup=keyboards.new_alert_kb())
        return
    text, markup = await _create(message.from_user.id, target, alert_service, rates, settings)
    await message.answer(text, reply_markup=markup)


@router.message(Command("alerts"))
@router.message(F.text == keyboards.BTN_ALERTS)
async def cmd_alerts(message: Message, db: Database, rates: RateService, settings: Settings) -> None:
    alerts = await db.list_alerts(message.from_user.id)
    price = _price(rates)
    await message.answer(
        texts.render_alerts_list(alerts, price, settings.max_alerts_per_user), reply_markup=keyboards.alerts_list_kb(alerts, price)
    )


@router.callback_query(Nav.filter(F.to == "alerts"))
async def cb_alerts(query: CallbackQuery, db: Database, rates: RateService, settings: Settings) -> None:
    alerts = await db.list_alerts(query.from_user.id)
    price = _price(rates)
    await safe_edit(query, texts.render_alerts_list(alerts, price, settings.max_alerts_per_user), keyboards.alerts_list_kb(alerts, price))
    await query.answer()


@router.callback_query(AlertCb.filter(F.act == "new"))
async def cb_new(query: CallbackQuery, rates: RateService) -> None:
    await safe_edit(query, texts.render_new_alert_prompt(_price(rates)), keyboards.new_alert_kb())
    await query.answer()


@router.callback_query(AlertCb.filter(F.act == "pct"))
async def cb_pct(query: CallbackQuery, callback_data: AlertCb, alert_service: AlertService, rates: RateService, settings: Settings) -> None:
    price = _price(rates)
    if price is None:
        await safe_edit(query, texts.ALERT_ERRORS["no_price"], keyboards.back_to_menu())
        await query.answer()
        return
    target = round(price * (1 + callback_data.val / 100), 2)
    text, markup = await _create(query.from_user.id, target, alert_service, rates, settings)
    await safe_edit(query, text, markup)
    await query.answer()


@router.message(StateFilter(None), _is_price_text)
async def text_price(message: Message, alert_service: AlertService, rates: RateService, settings: Settings) -> None:
    target = parse_price(message.text)
    assert target is not None
    text, markup = await _create(message.from_user.id, target, alert_service, rates, settings)
    await message.answer(text, reply_markup=markup)


@router.callback_query(AlertCb.filter(F.act == "open"))
async def cb_open(query: CallbackQuery, callback_data: AlertCb, db: Database, rates: RateService, settings: Settings) -> None:
    alert = await db.get_alert(callback_data.id, query.from_user.id)
    if alert is None:
        await query.answer("Алерт уже удалён", show_alert=True)
        return
    await safe_edit(query, texts.render_alert_detail(alert, _price(rates), settings.tz), keyboards.alert_detail_kb(alert))
    await query.answer()


@router.callback_query(AlertCb.filter(F.act == "rep"))
async def cb_toggle_repeat(query: CallbackQuery, callback_data: AlertCb, alert_service: AlertService, rates: RateService, settings: Settings) -> None:
    price = _price(rates)
    alert = await alert_service.toggle_repeat(callback_data.id, query.from_user.id, price)
    if alert is None:
        await query.answer("Алерт уже удалён", show_alert=True)
        return
    await safe_edit(query, texts.render_alert_detail(alert, price, settings.tz), keyboards.alert_detail_kb(alert))
    await query.answer("Готово")


@router.callback_query(AlertCb.filter(F.act == "del"))
async def cb_del_confirm(query: CallbackQuery, callback_data: AlertCb) -> None:
    kb = keyboards.confirm_kb(AlertCb(act="delyes", id=callback_data.id).pack(), Nav(to="alerts").pack())
    await safe_edit(query, "🗑 Удалить этот алерт?", kb)
    await query.answer()


@router.callback_query(AlertCb.filter(F.act == "delyes"))
async def cb_del(query: CallbackQuery, callback_data: AlertCb, db: Database, rates: RateService, settings: Settings) -> None:
    await db.delete_alert(callback_data.id, query.from_user.id)
    alerts = await db.list_alerts(query.from_user.id)
    price = _price(rates)
    text = "🗑 Удалено.\n\n" + texts.render_alerts_list(alerts, price, settings.max_alerts_per_user)
    await safe_edit(query, text, keyboards.alerts_list_kb(alerts, price))
    await query.answer()


@router.callback_query(AlertCb.filter(F.act == "clear"))
async def cb_clear_confirm(query: CallbackQuery) -> None:
    kb = keyboards.confirm_kb(AlertCb(act="clearyes").pack(), Nav(to="alerts").pack())
    await safe_edit(query, "🧹 Удалить все ваши алерты?", kb)
    await query.answer()


@router.callback_query(AlertCb.filter(F.act == "clearyes"))
async def cb_clear(query: CallbackQuery, db: Database, settings: Settings) -> None:
    n = await db.delete_all_alerts(query.from_user.id)
    text = f"🧹 Удалено алертов: {n}.\n\n" + texts.render_alerts_list([], None, settings.max_alerts_per_user)
    await safe_edit(query, text, keyboards.alerts_list_kb([], None))
    await query.answer()
