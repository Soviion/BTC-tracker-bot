"""Тесты AccessService на реальной PostgreSQL (см. tests/conftest.py::db)."""

from __future__ import annotations

import pytest

from bot.access import AccessService, RedeemStatus, RequestOutcome, Role, parse_invite_payload
from bot.db import Database


def make_access(db: Database, owner_ids: frozenset[int] = frozenset({111})) -> AccessService:
    return AccessService(db, owner_ids, invite_ttl_seconds=3600)


def test_parse_invite_payload() -> None:
    assert parse_invite_payload("inv_AbC123-_xyz") == "AbC123-_xyz"
    assert parse_invite_payload(None) is None
    assert parse_invite_payload("") is None
    assert parse_invite_payload("random") is None
    assert parse_invite_payload("inv_short") is None  # короче 8 символов — не похоже на наш код


async def test_owner_role_without_db_row(db: Database) -> None:
    access = make_access(db)
    assert access.is_owner(111) is True
    assert await access.role_of(111) is Role.OWNER  # доступ владельца не зависит от БД
    assert await access.role_of(222) is Role.NONE


async def test_grant_and_revoke(db: Database) -> None:
    access = make_access(db)
    user, created = await access.grant(222, username="bob", full_name="Bob")
    assert created and user.role == "user"
    assert await access.role_of(222) is Role.USER

    assert await access.revoke(111) is False  # владельца отозвать нельзя
    assert await access.role_of(111) is Role.OWNER
    assert await access.revoke(222) is True
    assert await access.role_of(222) is Role.NONE


async def test_invite_full_lifecycle(db: Database) -> None:
    access = make_access(db)
    invite = await access.create_invite(111)

    result = await access.redeem_invite(invite.code, 333, username="carol", full_name="Carol")
    assert result.status is RedeemStatus.OK
    assert result.user is not None and result.user.user_id == 333
    assert await access.role_of(333) is Role.USER

    again = await access.redeem_invite(invite.code, 444)
    assert again.status is RedeemStatus.USED

    already = await access.redeem_invite(invite.code, 333)  # 333 уже участник — свою же ссылку не тратит
    assert already.status is RedeemStatus.ALREADY_MEMBER


async def test_invite_expired(db: Database) -> None:
    access = make_access(db)
    invite = await db.create_invite(111, ttl_seconds=1, now=1_000)
    result = await access.redeem_invite(invite.code, 555)
    assert result.status is RedeemStatus.EXPIRED


async def test_invite_invalid_code(db: Database) -> None:
    access = make_access(db)
    result = await access.redeem_invite("totally-made-up-code", 666)
    assert result.status is RedeemStatus.INVALID


async def test_access_request_flow(db: Database) -> None:
    access = make_access(db)

    outcome = await access.register_request(777, username="dave", full_name="Dave")
    assert outcome is RequestOutcome.NEW
    outcome_again = await access.register_request(777, username="dave2", full_name="Dave2")
    assert outcome_again is RequestOutcome.PENDING

    user, created = await access.approve_request(777, by=111)
    assert created and user.username == "dave"  # данные взяты из исходного запроса
    assert await access.role_of(777) is Role.USER
    assert await db.get_request(777) is None  # запрос убран после выдачи доступа


async def test_access_request_denied_then_reapply(db: Database) -> None:
    access = make_access(db)
    await access.register_request(888, username="eve", full_name="Eve")
    denied = await access.deny_request(888)
    assert denied is True

    outcome = await access.register_request(888, username="eve", full_name="Eve")
    assert outcome is RequestOutcome.DENIED

    again = await access.deny_request(888)
    assert again is False  # уже отклонён — второй раз не «отклоняем» повторно
