"""Прямые тесты слоя bot/db.py на реальной PostgreSQL (см. tests/conftest.py::db)."""

from __future__ import annotations

import asyncio

import pytest

from bot.db import Database

NOW = 1_700_000_000  # фиксированный unix-timestamp — тесты не зависят от реального времени


# --------------------------------------------------------------------------- #
# Пользователи и владельцы
# --------------------------------------------------------------------------- #
async def test_sync_owners_promotes_and_demotes(db: Database) -> None:
    await db.sync_owners([111])
    assert (await db.get_user(111)).role == "owner"

    await db.sync_owners([111])  # повторный вызов идемпотентен
    assert (await db.get_user(111)).role == "owner"

    await db.sync_owners([333])  # смена владельца: 111 понижается, 333 повышается (и создаётся)
    assert (await db.get_user(111)).role == "user"
    assert (await db.get_user(333)).role == "owner"

    await db.sync_owners([])  # OWNER_ID очистили — все понижаются, никто не удаляется
    assert (await db.get_user(333)).role == "user"


async def test_add_user_is_idempotent_and_updates_profile(db: Database) -> None:
    user, created = await db.add_user(222, username="bob", full_name="Bob", added_by=111, now=NOW)
    assert created and user.role == "user"

    user2, created2 = await db.add_user(222, username="bobby", full_name="Bobby", now=NOW)
    assert not created2
    assert user2.username == "bobby" and user2.full_name == "Bobby"


async def test_touch_user_updates_last_seen_and_clears_blocked(db: Database) -> None:
    await db.add_user(222, full_name="Bob", now=NOW)
    await db.set_bot_blocked(222, True)
    assert (await db.get_user(222)).bot_blocked is True

    await db.touch_user(222, "bobby2", "Bobby Two", NOW + 10)
    user = await db.get_user(222)
    assert user.username == "bobby2" and user.bot_blocked is False


async def test_remove_user_is_idempotent(db: Database) -> None:
    await db.add_user(222, full_name="Bob", now=NOW)
    assert await db.remove_user(222) is True
    assert await db.remove_user(222) is False
    assert await db.get_user(222) is None


async def test_report_recipients_respects_blocked_and_opt_out(db: Database) -> None:
    await db.add_user(111, full_name="Owner", now=NOW)
    await db.add_user(222, full_name="Friend", now=NOW)
    await db.set_reports_off(222, ["08:00"])
    await db.add_user(333, full_name="Blocked", now=NOW)
    await db.set_bot_blocked(333, True)

    recipients_0800 = await db.report_recipients("08:00")
    recipients_1400 = await db.report_recipients("14:00")
    assert set(recipients_0800) == {111}
    assert set(recipients_1400) == {111, 222}


async def test_list_users_orders_owners_first(db: Database) -> None:
    await db.add_user(222, full_name="B", now=NOW)
    await db.sync_owners([111])
    users = await db.list_users()
    assert users[0].user_id == 111 and users[0].role == "owner"
    assert await db.count_users() == 2


# --------------------------------------------------------------------------- #
# Приглашения
# --------------------------------------------------------------------------- #
async def test_invite_lifecycle(db: Database) -> None:
    invite = await db.create_invite(111, ttl_seconds=3600, now=NOW)
    assert (await db.get_invite(invite.code)).used_by is None
    assert await db.count_open_invites(NOW) == 1

    assert await db.use_invite(invite.code, 444, now=NOW + 10) is True
    assert (await db.get_invite(invite.code)).used_by == 444


async def test_invite_cannot_be_used_twice_even_concurrently(db: Database) -> None:
    invite = await db.create_invite(111, ttl_seconds=3600, now=NOW)
    results = await asyncio.gather(*[db.use_invite(invite.code, uid, now=NOW) for uid in (201, 202, 203, 204, 205)])
    assert results.count(True) == 1  # ровно один «победитель» гонки за одноразовую ссылку


async def test_invite_expired_or_missing(db: Database) -> None:
    expired = await db.create_invite(111, ttl_seconds=1, now=NOW - 100)
    assert await db.use_invite(expired.code, 999, now=NOW) is False
    assert await db.use_invite("does-not-exist", 999, now=NOW) is False


async def test_revoke_open_invites_keeps_used_ones(db: Database) -> None:
    used = await db.create_invite(111, ttl_seconds=3600, now=NOW)
    await db.use_invite(used.code, 444, now=NOW)
    await db.create_invite(111, ttl_seconds=3600, now=NOW)  # остаётся открытым

    n = await db.revoke_open_invites()
    assert n == 1
    assert (await db.get_invite(used.code)).used_by == 444  # использованную ссылку не тронули


# --------------------------------------------------------------------------- #
# Запросы доступа
# --------------------------------------------------------------------------- #
async def test_access_request_created_once(db: Database) -> None:
    req, created = await db.create_request(777, "carol", "Carol", NOW)
    assert created and req.status == "pending"

    req2, created2 = await db.create_request(777, "carol2", "Carol2", NOW)
    assert not created2 and req2.username == "carol"  # дубль не перезаписывает исходные данные

    await db.set_request_status(777, "denied", NOW)
    assert (await db.get_request(777)).status == "denied"

    await db.delete_request(777)
    assert await db.get_request(777) is None


# --------------------------------------------------------------------------- #
# Алерты
# --------------------------------------------------------------------------- #
async def test_add_alert_dedups_active_alerts(db: Database) -> None:
    await db.add_user(444, full_name="Dave", now=NOW)
    a1, created1 = await db.add_alert(444, 90000.0, "above", 83000.0, NOW)
    assert created1

    a1_dup, created2 = await db.add_alert(444, 90000.0, "above", 83500.0, NOW)
    assert not created2 and a1_dup.id == a1.id  # цена «сейчас» другая — алерт всё равно тот же

    a2, created3 = await db.add_alert(444, 70000.0, "below", 83000.0, NOW)
    assert created3 and a2.id != a1.id


async def test_add_alert_concurrent_race_has_single_winner(db: Database) -> None:
    await db.add_user(888, full_name="Eve", now=NOW)
    results = await asyncio.gather(*[db.add_alert(888, 77777.0, "above", 83000.0, NOW) for _ in range(10)])
    created_flags = [created for _, created in results]
    assert created_flags.count(True) == 1
    assert await db.count_alerts(888) == 1


async def test_alert_trigger_and_mode_transitions(db: Database) -> None:
    await db.add_user(444, full_name="Dave", now=NOW)
    alert, _ = await db.add_alert(444, 90000.0, "above", 83000.0, NOW)

    await db.set_alert_armed(alert.id, False)
    assert (await db.get_alert(alert.id)).armed is False

    await db.set_alert_mode(alert.id, repeat=True, armed=True)
    reloaded = await db.get_alert(alert.id, 444)
    assert reloaded.repeat is True and reloaded.armed is True

    await db.mark_alert_triggered(alert.id, 91000.0, NOW, deactivate=False)
    triggered = await db.get_alert(alert.id)
    assert triggered.trigger_count == 1 and triggered.active is True and triggered.armed is False
    assert triggered.triggered_price == 91000.0

    await db.mark_alert_triggered(alert.id, 92000.0, NOW + 60, deactivate=True)
    deactivated = await db.get_alert(alert.id)
    assert deactivated.active is False and deactivated.trigger_count == 2


async def test_delete_alert_and_delete_all(db: Database) -> None:
    await db.add_user(444, full_name="Dave", now=NOW)
    a1, _ = await db.add_alert(444, 90000.0, "above", 83000.0, NOW)
    a2, _ = await db.add_alert(444, 70000.0, "below", 83000.0, NOW)

    assert await db.delete_alert(a1.id, 444) is True
    assert await db.delete_alert(a1.id, 444) is False  # уже удалён
    assert await db.delete_all_alerts(444) == 1  # остался только a2
    assert await db.list_alerts(444) == []
    assert a2.id > 0  # (просто чтобы a2 не выглядел неиспользуемым)


async def test_all_active_alerts_excludes_inactive(db: Database) -> None:
    await db.add_user(444, full_name="Dave", now=NOW)
    a1, _ = await db.add_alert(444, 90000.0, "above", 83000.0, NOW)
    a2, _ = await db.add_alert(444, 70000.0, "below", 83000.0, NOW)
    await db.mark_alert_triggered(a2.id, 69000.0, NOW, deactivate=True)

    active_ids = {a.id for a in await db.all_active_alerts()}
    assert active_ids == {a1.id}


# --------------------------------------------------------------------------- #
# История цен
# --------------------------------------------------------------------------- #
async def test_add_price_upserts_same_timestamp(db: Database) -> None:
    await db.add_price(NOW, 83000.0, 3.03, 3.5, 0.035)
    await db.add_price(NOW, 99999.0, None, None, None)  # тот же ts — перезаписывает, а не дублирует

    series = await db.price_series(NOW - 10)
    assert series == [(NOW, 99999.0)]


async def test_price_series_filters_by_window(db: Database) -> None:
    for i in range(5):
        await db.add_price(NOW - i * 60, 83000.0 + i, 3.03, 3.5, 0.035)
    series = await db.price_series(NOW - 120)
    assert len(series) == 3  # i=0,1,2 — остальные старше окна


# --------------------------------------------------------------------------- #
# Журнал сводок, мета, обслуживание
# --------------------------------------------------------------------------- #
async def test_claim_report_prevents_duplicates(db: Database) -> None:
    assert await db.claim_report("2026-01-01", "08:00", NOW) is True
    assert await db.claim_report("2026-01-01", "08:00", NOW) is False
    assert await db.claim_report("2026-01-01", "14:00", NOW) is True


async def test_meta_roundtrip_and_overwrite(db: Database) -> None:
    assert await db.get_meta("last_alive_at") is None
    await db.set_meta("last_alive_at", "123.456")
    assert await db.get_meta("last_alive_at") == "123.456"
    await db.set_meta("last_alive_at", "789.0")
    assert await db.get_meta("last_alive_at") == "789.0"


async def test_prune_does_not_raise_on_empty_db(db: Database) -> None:
    await db.prune(NOW, history_days=8)  # не должно бросать исключений даже без данных


async def test_prune_removes_old_history_and_keeps_recent(db: Database) -> None:
    await db.add_price(NOW - 10 * 86400, 80000.0, None, None, None)  # старше 8 дней
    await db.add_price(NOW - 1 * 86400, 83000.0, None, None, None)  # свежее

    await db.prune(NOW, history_days=8)

    series = await db.price_series(0)
    assert [ts for ts, _ in series] == [NOW - 86400]
