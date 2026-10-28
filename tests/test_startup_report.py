"""Стартовая сводка: ждёт интернет/Telegram, не спамит при перезапусках и не теряется при быстром падении."""

from __future__ import annotations

from bot.config import load_settings
from bot.db import Database
from bot.notify import BroadcastStats, SendResult
from bot.rates.models import BtcQuote, Snapshot
from bot.scheduler import STARTUP_META_KEY, SchedulerContext, deliver_startup_report

BOOT = 1_800_000_000.0


class FakeClock:
    def __init__(self, start: float = BOOT) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeRates:
    """`data_after` — после скольких обращений к refresh_if_stale «появляется интернет»:
    0 — данные есть сразу, None — не появятся никогда."""

    def __init__(self, clock: FakeClock, data_after: int | None = 0) -> None:
        self._clock = clock
        self._data_after = data_after
        self._polls = 0
        self.quote: BtcQuote | None = self._make() if data_after == 0 else None

    def _make(self) -> BtcQuote:
        return BtcQuote(price=83000.0, ts=self._clock.now(), sources_used=5, sources_total=5, spread_pct=0.1)

    def snapshot(self) -> Snapshot:
        return Snapshot(btc=self.quote)

    def fresh_btc(self, now: float | None = None) -> BtcQuote | None:
        return self.quote

    async def refresh_if_stale(self, max_age: float = 20.0) -> Snapshot:
        self._polls += 1
        if self.quote is None and self._data_after is not None and self._polls >= self._data_after:
            self.quote = self._make()
        return self.snapshot()


class FakeNotifier:
    """`script[uid]` — очередь результатов send() для этого получателя (по умолчанию всегда OK)."""

    def __init__(self, script: dict[int, list[SendResult]] | None = None) -> None:
        self.script = {uid: list(results) for uid, results in (script or {}).items()}
        self.sent: list[tuple[int, str]] = []
        self.notices: list[str] = []

    async def send(self, user_id, text, reply_markup=None, *, silent=False) -> SendResult:
        self.sent.append((user_id, text))
        queue = self.script.get(user_id)
        return queue.pop(0) if queue else SendResult.OK

    async def notify_owners(self, owner_ids, text, reply_markup=None) -> BroadcastStats:
        self.notices.append(text)
        return BroadcastStats(sent=len(list(owner_ids)), retry=0, failed=0)

    def texts_for(self, uid: int) -> list[str]:
        return [text for who, text in self.sent if who == uid]


def make_ctx(db: Database, clock: FakeClock, *, data_after: int | None = 0, notifier: FakeNotifier | None = None,
             **env: str) -> tuple[SchedulerContext, FakeNotifier]:
    notifier = notifier or FakeNotifier()
    settings = load_settings({
        "BOT_TOKEN": "123456789:AAEhBOweik6ad-exampleexampleexample",
        "OWNER_ID": "1",
        "DATABASE_URL": "postgresql://x:x@localhost/x",  # БД берём из фикстуры db
        **env,
    })
    ctx = SchedulerContext(settings=settings, db=db, rates=FakeRates(clock, data_after), notifier=notifier, version="test")
    return ctx, notifier


async def run(ctx: SchedulerContext, clock: FakeClock, *, prev_alive: float | None = None, deadline: float = 3600.0) -> bool:
    return await deliver_startup_report(
        ctx, prev_alive, BOOT, deadline=deadline, retry_every=15.0, sleep=clock.sleep, clock=clock.now,
    )


async def test_delivers_immediately_to_owner_and_users_but_not_blocked(db: Database) -> None:
    await db.add_user(2, full_name="Friend")
    await db.add_user(3, full_name="Blocked")
    await db.set_bot_blocked(3, True)
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock)

    assert await run(ctx, clock) is True

    assert "Бот запущен" in notifier.texts_for(1)[0]  # владельцу — расширенная версия
    assert "Бот снова в сети" in notifier.texts_for(2)[0]
    assert notifier.texts_for(3) == []
    assert clock.sleeps == []  # данные есть сразу — никакого ожидания
    assert await db.get_meta(STARTUP_META_KEY) == repr(BOOT)


async def test_owner_only_mode(db: Database) -> None:
    await db.add_user(2, full_name="Friend")
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock, STARTUP_REPORT="owner")

    assert await run(ctx, clock) is True
    assert [uid for uid, _ in notifier.sent] == [1]


async def test_off_sends_nothing(db: Database) -> None:
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock, STARTUP_REPORT="off")

    assert await run(ctx, clock) is False
    assert notifier.sent == [] and notifier.notices == []


async def test_nobody_to_notify_in_setup_mode(db: Database) -> None:
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock, OWNER_ID="")  # режим настройки: ни владельца, ни пользователей

    assert await run(ctx, clock) is False
    assert notifier.sent == [] and clock.sleeps == []


async def test_waits_for_data_then_delivers_and_warns_owner_once(db: Database) -> None:
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock, data_after=3)  # интернет «появляется» с третьей попытки

    assert await run(ctx, clock) is True

    assert len(notifier.notices) == 1 and "пока не отвечают" in notifier.notices[0]
    assert clock.sleeps == [15.0, 15.0]
    assert len(notifier.texts_for(1)) == 1  # сводка ушла один раз, когда данные появились
    assert "задержалась" not in notifier.texts_for(1)[0]  # 30 секунд — не повод для пояснения


async def test_long_wait_is_explained_in_owner_message_with_real_boot_time(db: Database) -> None:
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock, data_after=10)  # ~2.5 минуты без связи

    assert await run(ctx, clock, prev_alive=BOOT - 3 * 3600) is True

    text = notifier.texts_for(1)[0]
    assert "задержалась" in text
    assert "Простой: 3 ч" in text  # простой считается до момента ЗАПУСКА, а не до момента доставки


async def test_retries_while_telegram_is_unreachable(db: Database) -> None:
    clock = FakeClock()
    notifier = FakeNotifier({1: [SendResult.RETRY, SendResult.RETRY, SendResult.OK]})
    ctx, notifier = make_ctx(db, clock, notifier=notifier)

    assert await run(ctx, clock) is True
    assert len(notifier.texts_for(1)) == 3
    assert clock.sleeps == [15.0, 15.0]
    assert await db.get_meta(STARTUP_META_KEY) is not None


async def test_gives_up_at_deadline_and_does_not_mark_as_sent(db: Database) -> None:
    clock = FakeClock()
    notifier = FakeNotifier({1: [SendResult.RETRY] * 1000})
    ctx, notifier = make_ctx(db, clock, notifier=notifier)

    assert await run(ctx, clock, deadline=300.0) is False
    assert clock.now() - BOOT >= 300.0
    assert len(notifier.texts_for(1)) <= 21  # не бесконечный цикл
    assert await db.get_meta(STARTUP_META_KEY) is None  # следующий запуск попробует снова


async def test_permanent_failure_is_not_retried(db: Database) -> None:
    clock = FakeClock()
    notifier = FakeNotifier({1: [SendResult.FAILED]})  # например, владелец заблокировал бота
    ctx, notifier = make_ctx(db, clock, notifier=notifier)

    assert await run(ctx, clock) is False
    assert len(notifier.texts_for(1)) == 1 and clock.sleeps == []


async def test_skipped_when_previous_startup_report_was_just_sent(db: Database) -> None:
    await db.set_meta(STARTUP_META_KEY, repr(BOOT - 60))  # меньше STARTUP_MIN_GAP (180 с) назад
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock)

    assert await run(ctx, clock) is False
    assert notifier.sent == []


async def test_sent_when_previous_startup_report_was_long_ago(db: Database) -> None:
    await db.set_meta(STARTUP_META_KEY, repr(BOOT - 1000))
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock)

    assert await run(ctx, clock) is True


async def test_quick_restart_after_failed_attempt_still_sends(db: Database) -> None:
    """Регрессия: раньше антиспам считался от last_alive_at, и бот, упавший из-за отсутствия сети,
    при следующем быстром перезапуске пропускал сводку навсегда. Теперь важна только доставленная сводка."""
    clock = FakeClock()
    ctx, notifier = make_ctx(db, clock)

    assert await run(ctx, clock, prev_alive=BOOT - 10) is True  # «жив» был 10 секунд назад, но сводки не было
    assert "Перезапуск без простоя" in notifier.texts_for(1)[0]
