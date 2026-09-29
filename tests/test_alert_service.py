"""Тесты AlertService (создание с бизнес-правилами, переключение повтора) на реальной PostgreSQL."""

from __future__ import annotations

from bot.alerts import AlertService, CreateStatus
from bot.db import Database


async def test_create_above_and_below(db: Database) -> None:
    await db.add_user(1, full_name="A")
    service = AlertService(db, max_per_user=3)

    above = await service.create(1, 90000, 83000)
    assert above.status is CreateStatus.OK
    assert above.alert is not None and above.alert.direction == "above"

    below = await service.create(1, 70000, 83000)
    assert below.status is CreateStatus.OK
    assert below.alert.direction == "below"


async def test_create_rejects_no_price(db: Database) -> None:
    await db.add_user(1, full_name="A")
    service = AlertService(db)
    result = await service.create(1, 90000, None)
    assert result.status is CreateStatus.NO_PRICE


async def test_create_rejects_equal_to_current(db: Database) -> None:
    await db.add_user(1, full_name="A")
    service = AlertService(db)
    result = await service.create(1, 83000, 83000)
    assert result.status is CreateStatus.EQUALS_CURRENT


async def test_create_rejects_out_of_range(db: Database) -> None:
    await db.add_user(1, full_name="A")
    service = AlertService(db)
    too_low = await service.create(1, 83000 * 0.01, 83000)
    assert too_low.status is CreateStatus.OUT_OF_RANGE
    too_high = await service.create(1, 83000 * 25, 83000)
    assert too_high.status is CreateStatus.OUT_OF_RANGE


async def test_create_duplicate_returns_existing(db: Database) -> None:
    await db.add_user(1, full_name="A")
    service = AlertService(db)
    first = await service.create(1, 90000, 83000)
    second = await service.create(1, 90000, 83500)  # чуть другая текущая цена — тот же алерт
    assert second.status is CreateStatus.DUPLICATE
    assert second.alert.id == first.alert.id


async def test_create_respects_per_user_limit(db: Database) -> None:
    await db.add_user(1, full_name="A")
    service = AlertService(db, max_per_user=2)
    assert (await service.create(1, 90000, 83000)).status is CreateStatus.OK
    assert (await service.create(1, 95000, 83000)).status is CreateStatus.OK
    third = await service.create(1, 100000, 83000)
    assert third.status is CreateStatus.LIMIT


async def test_toggle_repeat_on_active_alert(db: Database) -> None:
    await db.add_user(1, full_name="A")
    service = AlertService(db)
    created = (await service.create(1, 90000, 83000)).alert
    assert created.repeat is False

    toggled = await service.toggle_repeat(created.id, 1, 83000)
    assert toggled.repeat is True
    toggled_back = await service.toggle_repeat(created.id, 1, 83000)
    assert toggled_back.repeat is False


async def test_toggle_repeat_reactivates_fired_alert_without_immediate_retrigger(db: Database) -> None:
    await db.add_user(1, full_name="A")
    service = AlertService(db)
    created = (await service.create(1, 90000, 83000)).alert
    await db.mark_alert_triggered(created.id, 91000, now=1_700_000_000, deactivate=True)
    fired = await db.get_alert(created.id, 1)
    assert fired.active is False

    # цена всё ещё выше цели — включаем повтор, но алерт не должен сработать немедленно
    reactivated = await service.toggle_repeat(created.id, 1, 91500)
    assert reactivated.active is True
    assert reactivated.repeat is True
    assert reactivated.armed is False  # «взведётся» только когда цена вернётся ниже цели


async def test_toggle_repeat_missing_alert_returns_none(db: Database) -> None:
    service = AlertService(db)
    assert await service.toggle_repeat(999999, 1, 83000) is None
