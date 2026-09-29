"""Кто может пользоваться ботом.

* Владелец — Telegram ID из OWNER_ID (.env). Доступ у него есть **всегда**: он не зависит от базы данных,
  его нельзя удалить или заблокировать через бота.
* Остальные попадают в список тремя способами: по одноразовой ссылке-приглашению, по одобренному
  запросу (человек нажал /start, владельцу пришла кнопка «Разрешить») или командой /adduser <id>.
"""

from __future__ import annotations

import enum
import re
import time
from dataclasses import dataclass
from typing import Callable

from .db import Database, Invite, User

INVITE_PREFIX = "inv_"
_INVITE_RE = re.compile(r"^inv_([A-Za-z0-9_-]{8,64})$")


class Role(str, enum.Enum):
    OWNER = "owner"
    USER = "user"
    NONE = "none"


class RedeemStatus(enum.Enum):
    OK = "ok"
    INVALID = "invalid"
    USED = "used"
    EXPIRED = "expired"
    ALREADY_MEMBER = "already_member"


class RequestOutcome(enum.Enum):
    NEW = "new"  # первый запрос — нужно уведомить владельца
    PENDING = "pending"  # уже ждёт решения
    DENIED = "denied"  # владелец отклонил


@dataclass(frozen=True)
class RedeemResult:
    status: RedeemStatus
    user: User | None = None
    invite: Invite | None = None


def parse_invite_payload(payload: str | None) -> str | None:
    """`/start inv_XXXX` → `XXXX`; всё остальное → None."""
    m = _INVITE_RE.match(payload or "")
    return m.group(1) if m else None


class AccessService:
    def __init__(
        self,
        db: Database,
        owner_ids: frozenset[int],
        *,
        invite_ttl_seconds: int = 24 * 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.owner_ids = owner_ids
        self.invite_ttl_seconds = invite_ttl_seconds
        self._clock = clock

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    async def role_of(self, user_id: int) -> Role:
        if user_id in self.owner_ids:  # владелец — без обращения к БД: доступ гарантирован
            return Role.OWNER
        return Role.USER if await self.db.get_user(user_id) else Role.NONE

    # ------------------------------------------------------------------ #
    # Прямая выдача / отзыв доступа
    # ------------------------------------------------------------------ #
    async def grant(
        self, user_id: int, *, username: str | None = None, full_name: str = "", added_by: int | None = None
    ) -> tuple[User, bool]:
        """Выдаёт доступ. Возвращает (пользователь, впервые_ли). Заодно закрывает висящий запрос."""
        user, created = await self.db.add_user(
            user_id, username=username, full_name=full_name, added_by=added_by, now=int(self._clock())
        )
        await self.db.delete_request(user_id)
        return user, created

    async def revoke(self, user_id: int) -> bool:
        """Отзывает доступ. Владельца отозвать нельзя. Алерты пользователя удаляются каскадно."""
        if self.is_owner(user_id):
            return False
        return await self.db.remove_user(user_id)

    # ------------------------------------------------------------------ #
    # Приглашения
    # ------------------------------------------------------------------ #
    async def create_invite(self, created_by: int) -> Invite:
        return await self.db.create_invite(created_by, self.invite_ttl_seconds, int(self._clock()))

    async def redeem_invite(
        self, code: str, user_id: int, *, username: str | None = None, full_name: str = ""
    ) -> RedeemResult:
        if await self.role_of(user_id) is not Role.NONE:
            return RedeemResult(RedeemStatus.ALREADY_MEMBER)  # ссылку не тратим
        now = int(self._clock())
        invite = await self.db.get_invite(code)
        if invite is None:
            return RedeemResult(RedeemStatus.INVALID)
        if invite.used_by is not None:
            return RedeemResult(RedeemStatus.USED, invite=invite)
        if invite.expires_at < now:
            return RedeemResult(RedeemStatus.EXPIRED, invite=invite)
        if not await self.db.use_invite(code, user_id, now):  # проиграли гонку за одноразовую ссылку
            fresh = await self.db.get_invite(code)
            used = fresh is not None and fresh.used_by is not None
            return RedeemResult(RedeemStatus.USED if used else RedeemStatus.EXPIRED, invite=fresh)
        user, _ = await self.grant(user_id, username=username, full_name=full_name, added_by=invite.created_by)
        return RedeemResult(RedeemStatus.OK, user=user, invite=invite)

    # ------------------------------------------------------------------ #
    # Запросы доступа
    # ------------------------------------------------------------------ #
    async def register_request(
        self, user_id: int, *, username: str | None = None, full_name: str = ""
    ) -> RequestOutcome:
        request, created = await self.db.create_request(user_id, username, full_name, int(self._clock()))
        if created:
            return RequestOutcome.NEW
        return RequestOutcome.PENDING if request.status == "pending" else RequestOutcome.DENIED

    async def approve_request(self, user_id: int, *, by: int) -> tuple[User, bool]:
        request = await self.db.get_request(user_id)
        return await self.grant(
            user_id,
            username=request.username if request else None,
            full_name=request.full_name if request else "",
            added_by=by,
        )

    async def deny_request(self, user_id: int) -> bool:
        """True — если был ожидающий запрос. Отклонённый пользователь больше не тревожит владельца."""
        request = await self.db.get_request(user_id)
        if request is None or request.status != "pending":
            return False
        await self.db.set_request_status(user_id, "denied", int(self._clock()))
        return True
