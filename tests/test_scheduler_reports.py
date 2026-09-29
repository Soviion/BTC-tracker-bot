"""Планирование ежедневных сводок: окно «прощения» опозданий и переход через полночь."""

from __future__ import annotations

import time
from datetime import datetime

from bot.config import load_settings
from bot.db import Database
from bot.notify import BroadcastStats
from bot.rates.models import BtcQuote, Snapshot
from bot.scheduler import SchedulerContext, _maybe_send_reports


class FakeRates:
    def __init__(self, snap: Snapshot) -> None:
        self._snap = snap

    def snapshot(self) -> Snapshot:
        return self._snap

    def fresh_btc(self, now: float | None = None):
        return self._snap.btc

    async def poll(self) -> Snapshot:
        return self._snap

    def source_overview(self):
        return []


class FakeNotifier:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[list[int], str]] = []

    async def broadcast(self, recipients, text, reply_markup=None, *, silent=False) -> BroadcastStats:
        recipients = list(recipients)
        self.broadcasts.append((recipients, text))
        return BroadcastStats(sent=len(recipients), retry=0, failed=0)

    async def notify_owners(self, owner_ids, text, reply_markup=None) -> BroadcastStats:
        return await self.broadcast(owner_ids, text, reply_markup)


def make_settings(**overrides: str):
    env = {
        "BOT_TOKEN": "123456789:AAEhBOweik6ad-exampleexampleexample",
        "OWNER_ID": "1",
        "DATABASE_URL": "postgresql://x:x@localhost/x",  # не используется — БД берём из фикстуры db
        "REPORT_TIMES": "08:00,23:59",
        "TZ_NAME": "Europe/Minsk",
    }
    env.update(overrides)
    return load_settings(env)


def make_ctx(db: Database, notifier: FakeNotifier | None = None) -> SchedulerContext:
    settings = make_settings()
    snap = Snapshot(btc=BtcQuote(price=83000.0, ts=time.time(), sources_used=5, sources_total=5, spread_pct=0.1))
    return SchedulerContext(settings=settings, db=db, rates=FakeRates(snap), notifier=notifier or FakeNotifier(), version="test")


async def test_sends_within_grace_and_does_not_duplicate(db: Database) -> None:
    await db.add_user(1, full_name="Owner")
    notifier = FakeNotifier()
    ctx = make_ctx(db, notifier)

    now = datetime.now(ctx.settings.tz).replace(hour=8, minute=5, second=0, microsecond=0)
    await _maybe_send_reports(ctx, now)
    assert len(notifier.broadcasts) == 1
    assert notifier.broadcasts[0][0] == [1]

    await _maybe_send_reports(ctx, now)  # тот же момент времени ещё раз (например, следующий тик цикла)
    assert len(notifier.broadcasts) == 1  # claim_report не даёт отправить дважды


async def test_too_late_is_skipped(db: Database) -> None:
    await db.add_user(1, full_name="Owner")
    notifier = FakeNotifier()
    ctx = make_ctx(db, notifier)
    # окно прощения — report_grace_minutes (по умолчанию 15 мин); полтора часа спустя — уже поздно
    now = datetime.now(ctx.settings.tz).replace(hour=9, minute=35, second=0, microsecond=0)
    await _maybe_send_reports(ctx, now)
    assert notifier.broadcasts == []


async def test_before_scheduled_time_is_skipped(db: Database) -> None:
    await db.add_user(1, full_name="Owner")
    notifier = FakeNotifier()
    ctx = make_ctx(db, notifier)
    now = datetime.now(ctx.settings.tz).replace(hour=7, minute=59, second=0, microsecond=0)
    await _maybe_send_reports(ctx, now)
    assert notifier.broadcasts == []


async def test_survives_midnight_for_2359_slot(db: Database) -> None:
    """Бот «упал» в 23:58 и поднялся в 00:10 — вчерашняя сводка за 23:59 должна всё равно дойти."""
    await db.add_user(1, full_name="Owner")
    notifier = FakeNotifier()
    ctx = make_ctx(db, notifier)

    now = datetime.now(ctx.settings.tz).replace(hour=0, minute=10, second=0, microsecond=0)
    await _maybe_send_reports(ctx, now)

    assert len(notifier.broadcasts) == 1
    text = notifier.broadcasts[0][1]
    assert "23:59" in text
    assert "опозданием" in text  # late_minutes=10 >= 3 — текст должен предупредить об опоздании


async def test_respects_per_user_report_off_setting(db: Database) -> None:
    await db.add_user(1, full_name="Owner")
    await db.add_user(2, full_name="Friend")
    await db.set_reports_off(2, ["08:00"])
    notifier = FakeNotifier()
    ctx = make_ctx(db, notifier)

    now = datetime.now(ctx.settings.tz).replace(hour=8, minute=0, second=0, microsecond=0)
    await _maybe_send_reports(ctx, now)

    assert len(notifier.broadcasts) == 1
    recipients = notifier.broadcasts[0][0]
    assert 1 in recipients and 2 not in recipients
