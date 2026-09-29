"""Хранилище на PostgreSQL (asyncpg): пользователи, приглашения, запросы доступа, алерты, история цен.

БД — внешняя, на сервере пользователя (см. DATABASE_URL в .env). Публичный интерфейс класса Database
(датаклассы и сигнатуры методов) — стабильный контракт: от него зависят access.py, alerts.py, notify.py,
history.py, texts.py/keyboards.py. Меняя SQL внутри, не меняйте то, что эти модули видят снаружи.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Iterable

import asyncpg

log = logging.getLogger(__name__)

# Предел колонок user_id/added_by (BIGINT) — валидаторы ввода (config.py, handlers/admin.py) проверяют
# Telegram ID против этой границы заранее, чтобы не ловить необработанное исключение asyncpg.
BIGINT_MAX = 2**63 - 1

# Каждая миграция — самостоятельный DDL-скрипт; применяются по порядку, версия хранится в schema_migrations.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE users (
        user_id     BIGINT PRIMARY KEY,
        username    TEXT,
        full_name   TEXT    NOT NULL DEFAULT '',
        role        TEXT    NOT NULL DEFAULT 'user',      -- owner | user
        added_by    BIGINT,
        added_at    BIGINT  NOT NULL,
        last_seen   BIGINT,
        bot_blocked BOOLEAN NOT NULL DEFAULT FALSE,        -- пользователь заблокировал бота
        reports_off TEXT    NOT NULL DEFAULT ''            -- отключённые слоты сводок: "08:00,14:00"
    );

    CREATE TABLE invites (
        code       TEXT PRIMARY KEY,
        created_by BIGINT  NOT NULL,
        created_at BIGINT  NOT NULL,
        expires_at BIGINT  NOT NULL,
        used_by    BIGINT,
        used_at    BIGINT
    );

    CREATE TABLE access_requests (
        user_id    BIGINT PRIMARY KEY,
        username   TEXT,
        full_name  TEXT   NOT NULL DEFAULT '',
        status     TEXT   NOT NULL DEFAULT 'pending',      -- pending | denied
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    );

    CREATE TABLE alerts (
        id              BIGSERIAL PRIMARY KEY,
        user_id         BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        target          DOUBLE PRECISION NOT NULL,
        direction       TEXT    NOT NULL CHECK (direction IN ('above', 'below')),
        repeat          BOOLEAN NOT NULL DEFAULT FALSE,
        armed           BOOLEAN NOT NULL DEFAULT TRUE,
        active          BOOLEAN NOT NULL DEFAULT TRUE,
        created_at      BIGINT  NOT NULL,
        created_price   DOUBLE PRECISION,
        triggered_at    BIGINT,
        triggered_price DOUBLE PRECISION,
        trigger_count   INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_alerts_user   ON alerts(user_id, active);
    CREATE INDEX idx_alerts_active ON alerts(active);
    -- Не более одного активного алерта на (пользователь, направление, цель) — дедуп на уровне БД,
    -- без отдельной блокировки на стороне приложения (target уже округлён вызывающим кодом до центов).
    CREATE UNIQUE INDEX idx_alerts_unique_active ON alerts(user_id, direction, target) WHERE active;

    CREATE TABLE price_history (
        ts      BIGINT PRIMARY KEY,
        btc_usd DOUBLE PRECISION,
        usd_byn DOUBLE PRECISION,
        eur_byn DOUBLE PRECISION,
        rub_byn DOUBLE PRECISION
    );

    CREATE TABLE report_log (
        day     TEXT   NOT NULL,
        slot    TEXT   NOT NULL,
        sent_at BIGINT NOT NULL,
        PRIMARY KEY (day, slot)
    );

    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
)


# --------------------------------------------------------------------------- #
# Модели строк
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class User:
    user_id: int
    username: str | None
    full_name: str
    role: str
    added_by: int | None
    added_at: int
    last_seen: int | None
    bot_blocked: bool
    reports_off: frozenset[str]


@dataclass(frozen=True)
class Invite:
    code: str
    created_by: int
    created_at: int
    expires_at: int
    used_by: int | None
    used_at: int | None


@dataclass(frozen=True)
class AccessRequest:
    user_id: int
    username: str | None
    full_name: str
    status: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Alert:
    id: int
    user_id: int
    target: float
    direction: str  # above | below
    repeat: bool
    armed: bool
    active: bool
    created_at: int
    created_price: float | None
    triggered_at: int | None
    triggered_price: float | None
    trigger_count: int


def _user(r: asyncpg.Record) -> User:
    return User(
        user_id=r["user_id"], username=r["username"], full_name=r["full_name"], role=r["role"],
        added_by=r["added_by"], added_at=r["added_at"], last_seen=r["last_seen"],
        bot_blocked=r["bot_blocked"],
        reports_off=frozenset(s for s in r["reports_off"].split(",") if s),
    )


def _alert(r: asyncpg.Record) -> Alert:
    return Alert(
        id=r["id"], user_id=r["user_id"], target=r["target"], direction=r["direction"],
        repeat=r["repeat"], armed=r["armed"], active=r["active"],
        created_at=r["created_at"], created_price=r["created_price"], triggered_at=r["triggered_at"],
        triggered_price=r["triggered_price"], trigger_count=r["trigger_count"],
    )


def _rowcount(tag: str) -> int:
    """«UPDATE 3» / «INSERT 0 1» / «DELETE 0» → число затронутых строк."""
    parts = tag.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


class Database:
    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        ssl: str | None = None,
        disable_statement_cache: bool = False,
        connect_timeout: float = 10.0,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._ssl = ssl
        self._statement_cache_size = 0 if disable_statement_cache else 100
        self._connect_timeout = connect_timeout
        self._pool: asyncpg.Pool | None = None

    # ------------------------------------------------------------------ #
    # Подключение и миграции
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        # timeout — на КАЖДУЮ попытку соединения: по умолчанию у asyncpg 60с, из-за чего недоступная
        # БД (например, порт просто закрыт) обнаруживалась бы неприлично долго. Терпеливое ожидание
        # медленно поднимающейся БД после перезагрузки сервера обеспечивает внешний цикл повторов
        # в main.py (_connect_db_with_retry), а не большой таймаут здесь.
        try:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                ssl=self._ssl,
                statement_cache_size=self._statement_cache_size,
                command_timeout=30,
                timeout=self._connect_timeout,
            )
        except (OSError, asyncpg.PostgresError) as exc:
            raise ConnectionError(
                f"Не удалось подключиться к PostgreSQL по DATABASE_URL: {exc}. "
                "Проверьте, что сервер БД запущен и доступен, а логин/пароль/имя базы верны."
            ) from exc
        await self._migrate()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def _p(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("База данных не подключена (вызовите connect())")
        return self._pool

    async def _migrate(self) -> None:
        async with self._p.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at BIGINT NOT NULL)"
            )
            version = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                log.info("применяю миграцию БД v%d", number)
                async with conn.transaction():
                    await conn.execute(script)
                    await conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES ($1, $2)", number, int(time.time())
                    )

    # ------------------------------------------------------------------ #
    # Пользователи
    # ------------------------------------------------------------------ #
    async def sync_owners(self, owner_ids: Iterable[int], now: int | None = None) -> None:
        """Приводит роли в БД в соответствие с OWNER_ID из настроек."""
        now = int(time.time()) if now is None else now
        ids = sorted(set(owner_ids))
        async with self._p.acquire() as conn, conn.transaction():
            for uid in ids:
                await conn.execute(
                    "INSERT INTO users(user_id, full_name, role, added_at) VALUES ($1, '', 'owner', $2) "
                    "ON CONFLICT (user_id) DO UPDATE SET role = 'owner'",
                    uid, now,
                )
            if ids:
                await conn.execute(
                    "UPDATE users SET role = 'user' WHERE role = 'owner' AND user_id != ALL($1::bigint[])", ids
                )
            else:
                await conn.execute("UPDATE users SET role = 'user' WHERE role = 'owner'")

    async def get_user(self, user_id: int) -> User | None:
        row = await self._p.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return _user(row) if row else None

    async def list_users(self) -> list[User]:
        rows = await self._p.fetch("SELECT * FROM users ORDER BY (role = 'owner') DESC, added_at, user_id")
        return [_user(r) for r in rows]

    async def count_users(self) -> int:
        return await self._p.fetchval("SELECT COUNT(*) FROM users")

    async def add_user(
        self, user_id: int, *, username: str | None = None, full_name: str = "",
        added_by: int | None = None, now: int | None = None,
    ) -> tuple[User, bool]:
        """Выдаёт доступ. Возвращает (пользователь, создан_ли_новый). Повторный вызов безопасен."""
        now = int(time.time()) if now is None else now
        tag = await self._p.execute(
            "INSERT INTO users(user_id, username, full_name, role, added_by, added_at) "
            "VALUES ($1, $2, $3, 'user', $4, $5) ON CONFLICT (user_id) DO NOTHING",
            user_id, username, full_name, added_by, now,
        )
        created = _rowcount(tag) == 1
        if not created and (username or full_name):
            await self._p.execute(
                "UPDATE users SET username = COALESCE($1, username), "
                "full_name = CASE WHEN $2 != '' THEN $2 ELSE full_name END WHERE user_id = $3",
                username, full_name, user_id,
            )
        user = await self.get_user(user_id)
        assert user is not None
        return user, created

    async def touch_user(self, user_id: int, username: str | None, full_name: str, now: int | None = None) -> None:
        """Обновляет профиль/«был в сети» и снимает пометку «заблокировал бота»."""
        now = int(time.time()) if now is None else now
        await self._p.execute(
            "UPDATE users SET username = $1, full_name = $2, last_seen = $3, bot_blocked = FALSE WHERE user_id = $4",
            username, full_name, now, user_id,
        )

    async def remove_user(self, user_id: int) -> bool:
        tag = await self._p.execute("DELETE FROM users WHERE user_id = $1", user_id)
        return _rowcount(tag) > 0

    async def set_bot_blocked(self, user_id: int, blocked: bool) -> None:
        await self._p.execute("UPDATE users SET bot_blocked = $1 WHERE user_id = $2", blocked, user_id)

    async def set_reports_off(self, user_id: int, slots: Iterable[str]) -> None:
        await self._p.execute(
            "UPDATE users SET reports_off = $1 WHERE user_id = $2", ",".join(sorted(set(slots))), user_id
        )

    async def report_recipients(self, slot: str) -> list[int]:
        """Кому слать сводку этого слота: есть доступ, не заблокировал бота, слот не выключен."""
        rows = await self._p.fetch("SELECT user_id, reports_off FROM users WHERE bot_blocked = FALSE")
        return [r["user_id"] for r in rows if slot not in r["reports_off"].split(",")]

    # ------------------------------------------------------------------ #
    # Приглашения
    # ------------------------------------------------------------------ #
    async def create_invite(self, created_by: int, ttl_seconds: int, now: int | None = None) -> Invite:
        now = int(time.time()) if now is None else now
        code = secrets.token_urlsafe(12)  # 96 бит энтропии, только [A-Za-z0-9_-]
        await self._p.execute(
            "INSERT INTO invites(code, created_by, created_at, expires_at) VALUES ($1, $2, $3, $4)",
            code, created_by, now, now + ttl_seconds,
        )
        return Invite(code, created_by, now, now + ttl_seconds, None, None)

    async def get_invite(self, code: str) -> Invite | None:
        row = await self._p.fetchrow("SELECT * FROM invites WHERE code = $1", code)
        return Invite(**dict(row)) if row else None

    async def use_invite(self, code: str, user_id: int, now: int | None = None) -> bool:
        """Атомарно «сжигает» приглашение. False — если оно уже использовано/просрочено/не существует."""
        now = int(time.time()) if now is None else now
        tag = await self._p.execute(
            "UPDATE invites SET used_by = $1, used_at = $2 WHERE code = $3 AND used_by IS NULL AND expires_at >= $4",
            user_id, now, code, now,
        )
        return _rowcount(tag) == 1

    async def revoke_open_invites(self) -> int:
        tag = await self._p.execute("DELETE FROM invites WHERE used_by IS NULL")
        return _rowcount(tag)

    async def count_open_invites(self, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        return await self._p.fetchval("SELECT COUNT(*) FROM invites WHERE used_by IS NULL AND expires_at >= $1", now)

    # ------------------------------------------------------------------ #
    # Запросы доступа
    # ------------------------------------------------------------------ #
    async def get_request(self, user_id: int) -> AccessRequest | None:
        row = await self._p.fetchrow("SELECT * FROM access_requests WHERE user_id = $1", user_id)
        return AccessRequest(**dict(row)) if row else None

    async def create_request(
        self, user_id: int, username: str | None, full_name: str, now: int | None = None
    ) -> tuple[AccessRequest, bool]:
        """Возвращает (запрос, создан_ли_новый). Уже существующий запрос не перезаписывается."""
        now = int(time.time()) if now is None else now
        tag = await self._p.execute(
            "INSERT INTO access_requests(user_id, username, full_name, status, created_at, updated_at) "
            "VALUES ($1, $2, $3, 'pending', $4, $4) ON CONFLICT (user_id) DO NOTHING",
            user_id, username, full_name, now,
        )
        request = await self.get_request(user_id)
        assert request is not None
        return request, _rowcount(tag) == 1

    async def set_request_status(self, user_id: int, status: str, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        await self._p.execute(
            "UPDATE access_requests SET status = $1, updated_at = $2 WHERE user_id = $3", status, now, user_id
        )

    async def delete_request(self, user_id: int) -> None:
        await self._p.execute("DELETE FROM access_requests WHERE user_id = $1", user_id)

    # ------------------------------------------------------------------ #
    # Алерты
    # ------------------------------------------------------------------ #
    async def add_alert(
        self, user_id: int, target: float, direction: str, price_now: float | None, now: int | None = None
    ) -> tuple[Alert, bool]:
        """Создаёт алерт. Если такой же уже активен — возвращает его и False (дедуп на уровне БД)."""
        now = int(time.time()) if now is None else now
        row = await self._p.fetchrow(
            "INSERT INTO alerts(user_id, target, direction, created_at, created_price) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (user_id, direction, target) WHERE active DO NOTHING RETURNING *",
            user_id, target, direction, now, price_now,
        )
        if row is not None:
            return _alert(row), True
        existing = await self._p.fetchrow(
            "SELECT * FROM alerts WHERE user_id = $1 AND direction = $2 AND target = $3 AND active",
            user_id, direction, target,
        )
        assert existing is not None, "ON CONFLICT сработал, но дублирующий активный алерт не найден"
        return _alert(existing), False

    async def get_alert(self, alert_id: int, user_id: int | None = None) -> Alert | None:
        if user_id is None:
            row = await self._p.fetchrow("SELECT * FROM alerts WHERE id = $1", alert_id)
        else:
            row = await self._p.fetchrow("SELECT * FROM alerts WHERE id = $1 AND user_id = $2", alert_id, user_id)
        return _alert(row) if row else None

    async def list_alerts(self, user_id: int) -> list[Alert]:
        rows = await self._p.fetch(
            "SELECT * FROM alerts WHERE user_id = $1 AND active ORDER BY target DESC", user_id
        )
        return [_alert(r) for r in rows]

    async def count_alerts(self, user_id: int | None = None) -> int:
        if user_id is None:
            return await self._p.fetchval("SELECT COUNT(*) FROM alerts WHERE active")
        return await self._p.fetchval("SELECT COUNT(*) FROM alerts WHERE active AND user_id = $1", user_id)

    async def all_active_alerts(self) -> list[Alert]:
        rows = await self._p.fetch("SELECT * FROM alerts WHERE active ORDER BY id")
        return [_alert(r) for r in rows]

    async def delete_alert(self, alert_id: int, user_id: int) -> bool:
        tag = await self._p.execute("DELETE FROM alerts WHERE id = $1 AND user_id = $2", alert_id, user_id)
        return _rowcount(tag) > 0

    async def delete_all_alerts(self, user_id: int) -> int:
        tag = await self._p.execute("DELETE FROM alerts WHERE user_id = $1 AND active", user_id)
        return _rowcount(tag)

    async def set_alert_mode(self, alert_id: int, *, repeat: bool, armed: bool, active: bool = True) -> None:
        await self._p.execute(
            "UPDATE alerts SET repeat = $1, armed = $2, active = $3 WHERE id = $4", repeat, armed, active, alert_id
        )

    async def set_alert_armed(self, alert_id: int, armed: bool) -> None:
        await self._p.execute("UPDATE alerts SET armed = $1 WHERE id = $2", armed, alert_id)

    async def mark_alert_triggered(self, alert_id: int, price: float, now: int, *, deactivate: bool) -> None:
        """Фиксирует срабатывание. Разовый алерт выключается, повторяющийся «разряжается» до отката цены."""
        await self._p.execute(
            "UPDATE alerts SET triggered_at = $1, triggered_price = $2, trigger_count = trigger_count + 1, "
            "active = CASE WHEN $3 THEN FALSE ELSE active END, armed = FALSE WHERE id = $4",
            now, price, deactivate, alert_id,
        )

    # ------------------------------------------------------------------ #
    # История цен и журнал сводок
    # ------------------------------------------------------------------ #
    async def add_price(
        self, ts: int, btc_usd: float | None, usd_byn: float | None, eur_byn: float | None, rub_byn: float | None
    ) -> None:
        await self._p.execute(
            "INSERT INTO price_history(ts, btc_usd, usd_byn, eur_byn, rub_byn) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (ts) DO UPDATE SET btc_usd = $2, usd_byn = $3, eur_byn = $4, rub_byn = $5",
            ts, btc_usd, usd_byn, eur_byn, rub_byn,
        )

    async def price_series(self, since_ts: int) -> list[tuple[int, float]]:
        rows = await self._p.fetch(
            "SELECT ts, btc_usd FROM price_history WHERE ts >= $1 AND btc_usd IS NOT NULL ORDER BY ts", since_ts
        )
        return [(r["ts"], r["btc_usd"]) for r in rows]

    async def claim_report(self, day: str, slot: str, now: int | None = None) -> bool:
        """True — слот за этот день ещё не отправлялся (и теперь помечен). Защита от дублей при перезапусках."""
        now = int(time.time()) if now is None else now
        tag = await self._p.execute(
            "INSERT INTO report_log(day, slot, sent_at) VALUES ($1, $2, $3) ON CONFLICT (day, slot) DO NOTHING",
            day, slot, now,
        )
        return _rowcount(tag) == 1

    async def prune(self, now: int | None = None, *, history_days: int = 8) -> None:
        """Чистка: старая история цен, использованные/просроченные приглашения, давно сработавшие алерты."""
        now = int(time.time()) if now is None else now
        day = 86400
        async with self._p.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM price_history WHERE ts < $1", now - history_days * day)
            await conn.execute(
                "DELETE FROM invites WHERE (used_by IS NOT NULL AND used_at < $1) OR expires_at < $2",
                now - 30 * day, now - 7 * day,
            )
            await conn.execute("DELETE FROM alerts WHERE NOT active AND triggered_at < $1", now - 7 * day)
            await conn.execute("DELETE FROM report_log WHERE sent_at < $1", now - 30 * day)

    # ------------------------------------------------------------------ #
    # Мета
    # ------------------------------------------------------------------ #
    async def get_meta(self, key: str) -> str | None:
        return await self._p.fetchval("SELECT value FROM meta WHERE key = $1", key)

    async def set_meta(self, key: str, value: str) -> None:
        await self._p.execute(
            "INSERT INTO meta(key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2", key, value
        )
