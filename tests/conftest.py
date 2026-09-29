"""Общие фикстуры. Тесты, которым нужна БД, аккуратно пропускаются, если PostgreSQL недоступна.

Доступность БД проверяется один раз за весь прогон и кладётся в обычный (не asyncio-) словарь —
поэтому при недоступной БД все зависимые тесты пропускаются быстро, а не платят по отдельности за
таймаут соединения. Сам `Database` с пулом при этом создаётся заново в КАЖДОМ тесте: pytest-asyncio
может давать разным тестам разные event loop'ы, и один и тот же пул на всех тестов ловит
«Future attached to a different loop» — поэтому session-scope годится только для дешёвой проверки.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from bot.db import Database

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://btc_bot:test_pw@localhost:55432/btc_tracker")

_TABLES = "users, invites, access_requests, alerts, price_history, report_log, meta"
_availability: dict[str, bool] = {}


async def _postgres_available() -> bool:
    if "ok" not in _availability:
        try:
            conn = await asyncpg.connect(TEST_DSN, timeout=5.0)
        except (OSError, asyncpg.PostgresError):
            _availability["ok"] = False
        else:
            await conn.close()
            _availability["ok"] = True
    return _availability["ok"]


@pytest_asyncio.fixture
async def db() -> Database:
    if not await _postgres_available():
        pytest.skip(f"тестовая PostgreSQL недоступна ({TEST_DSN})")

    database = Database(TEST_DSN, min_size=1, max_size=5, connect_timeout=5.0)
    await database.connect()

    conn = await asyncpg.connect(TEST_DSN, timeout=5.0)
    try:
        await conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")
    finally:
        await conn.close()

    yield database
    await database.close()
