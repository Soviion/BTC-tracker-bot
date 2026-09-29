"""Опрос всех источников, защита от плохих данных и хранение актуальных курсов."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Sequence

from .aggregate import aggregate_prices
from .btc_providers import DEFAULT_BTC_PROVIDERS, BtcProvider
from .fiat_providers import DEFAULT_FIAT_PROVIDERS, FiatProvider
from .http import Fetcher, describe_error
from .models import BankRates, BtcQuote, FiatRates, Snapshot, SourceStatus

log = logging.getLogger(__name__)

Provider = BtcProvider | FiatProvider


class RateService:
    def __init__(
        self,
        fetcher: Fetcher,
        *,
        btc_providers: Sequence[BtcProvider] = DEFAULT_BTC_PROVIDERS,
        fiat_providers: Sequence[FiatProvider] = DEFAULT_FIAT_PROVIDERS,
        deadline: float = 10.0,  # общий лимит на один источник (с учётом зеркал)
        stale_after: float = 180.0,
        max_dev_pct: float = 3.0,  # отсев источника, отклонившегося от медианы больше чем на 3%
        jump_pct: float = 10.0,  # скачок цены за одну минуту больше 10% требует подтверждения
        official_grace: float = 900.0,  # сколько терпим сбой НБРБ, прежде чем взять резервный курс
        fallback_interval: float = 600.0,  # как часто трогать резервные (лимитированные) API
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetcher = fetcher
        self.btc_providers = tuple(btc_providers)
        self.fiat_providers = tuple(fiat_providers)
        self._deadline = deadline
        self.stale_after = stale_after
        self._max_dev_pct = max_dev_pct
        self._jump_pct = jump_pct
        self._official_grace = official_grace
        self._fallback_interval = fallback_interval
        self._clock = clock

        self.btc: BtcQuote | None = None
        self.fiat: FiatRates | None = None
        self.bank: BankRates | None = None
        self.statuses: dict[str, SourceStatus] = {}
        self.last_poll_at: float | None = None

        self._pending_jump: float | None = None
        self._last_fallback_try = 0.0
        self._fallback_cache: FiatRates | None = None
        self._poll_lock = asyncio.Lock()  # опрос по расписанию и по кнопке не должны идти одновременно

    # ------------------------------------------------------------------ #
    # Публичный интерфейс
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Snapshot:
        return Snapshot(btc=self.btc, fiat=self.fiat, bank=self.bank)

    def fresh_btc(self, now: float | None = None) -> BtcQuote | None:
        """Котировка BTC, если она не устарела (иначе None — алерты по ней не срабатывают)."""
        now = self._clock() if now is None else now
        if self.btc is not None and now - self.btc.ts <= self.stale_after:
            return self.btc
        return None

    def source_overview(self) -> list[tuple[Provider, SourceStatus | None]]:
        """Все источники (в порядке подключения) с их последним статусом."""
        providers: list[Provider] = [*self.btc_providers, *self.fiat_providers]
        return [(p, self.statuses.get(p.key)) for p in providers]

    async def poll(self) -> Snapshot:
        """Один цикл опроса всех источников. Никогда не бросает исключений."""
        async with self._poll_lock:
            return await self._poll_locked()

    async def refresh_if_stale(self, max_age: float = 20.0) -> Snapshot:
        """Для кнопки «Обновить»: опрашивает источники, только если данным больше `max_age` секунд."""
        async with self._poll_lock:
            if self.last_poll_at is None or self._clock() - self.last_poll_at > max_age:
                return await self._poll_locked()
            return self.snapshot()

    async def _poll_locked(self) -> Snapshot:
        now = self._clock()
        await asyncio.gather(self._guard(self._poll_btc, now), self._guard(self._poll_fiat, now))
        self.last_poll_at = now
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # BTC
    # ------------------------------------------------------------------ #
    async def _poll_btc(self, now: float) -> None:
        results = await asyncio.gather(*(self._call(p, lambda p=p: p.fetch(self._fetcher), now) for p in self.btc_providers))
        prices = {p.key: v for p, v in zip(self.btc_providers, results) if isinstance(v, float)}
        if not prices:
            return  # старая котировка остаётся и «стареет» — потребители увидят это по ts

        prev = self.btc
        reference = prev.price if prev else None
        agg = aggregate_prices(prices, reference=reference, max_dev_pct=self._max_dev_pct)
        assert agg is not None

        # Защита от мусорных данных: BTC не прыгает на 10%+ за минуту. Резкий скачок принимаем,
        # только если следующий опрос его подтвердил (или если старая цена уже давно не обновлялась).
        if prev is not None and now - prev.ts < 600 and abs(agg.price - prev.price) / prev.price * 100 > self._jump_pct:
            pending = self._pending_jump
            if pending is not None and abs(agg.price - pending) / pending * 100 <= 2.0:
                log.warning("резкое движение BTC подтверждено: %.0f → %.0f", prev.price, agg.price)
            else:
                log.warning("подозрительный скачок BTC %.0f → %.0f — ждём подтверждения", prev.price, agg.price)
                self._pending_jump = agg.price
                return
        self._pending_jump = None

        self.btc = BtcQuote(
            price=agg.price, ts=now, sources_used=len(agg.used), sources_total=len(self.btc_providers),
            spread_pct=agg.spread_pct, prices=dict(agg.used),
        )
        if agg.rejected:
            log.info("отброшены выбросы: %s (медиана %.0f)", {k: round(v) for k, v in agg.rejected.items()}, agg.price)

    # ------------------------------------------------------------------ #
    # Курсы BYN
    # ------------------------------------------------------------------ #
    async def _poll_fiat(self, now: float) -> None:
        primary = [p for p in self.fiat_providers if p.kind in ("official", "bank")]
        results = await asyncio.gather(*(self._call(p, lambda p=p: p.fetch(self._fetcher, now), now) for p in primary))

        official: FiatRates | None = None
        for provider, result in zip(primary, results):
            if isinstance(result, FiatRates) and provider.kind == "official":
                official = result
            elif isinstance(result, BankRates) and provider.kind == "bank":
                self.bank = result

        if official is not None:
            self.fiat = official
            return
        # НБРБ не ответил. Небольшой сбой пережидаем на старом официальном курсе,
        # затянувшийся — переключаемся на международный (с пометкой в сообщениях).
        recent_official = self.fiat is not None and self.fiat.official and now - self.fiat.ts <= self._official_grace
        if not recent_official:
            fallback = await self._fallback_fiat(now)
            if fallback is not None:
                self.fiat = fallback

    async def _fallback_fiat(self, now: float) -> FiatRates | None:
        if now - self._last_fallback_try < self._fallback_interval:
            return self._fallback_cache
        self._last_fallback_try = now
        for provider in (p for p in self.fiat_providers if p.kind == "fallback"):
            result = await self._call(provider, lambda p=provider: p.fetch(self._fetcher, now), now)
            if isinstance(result, FiatRates):
                self._fallback_cache = result
                return result
        return self._fallback_cache

    # ------------------------------------------------------------------ #
    # Внутренняя кухня
    # ------------------------------------------------------------------ #
    async def _guard(self, fn: Callable[[float], Awaitable[None]], now: float) -> None:
        try:
            await fn(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — опрос не должен падать целиком
            log.exception("неожиданная ошибка при опросе (%s)", getattr(fn, "__name__", fn))

    async def _call(self, provider: Provider, factory: Callable[[], Awaitable[Any]], now: float) -> Any:
        started = time.monotonic()
        try:
            value = await asyncio.wait_for(factory(), timeout=self._deadline)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._record(provider, now, ok=False, latency_ms=self._ms(started), error=describe_error(exc))
            return None
        self._record(provider, now, ok=True, latency_ms=self._ms(started), value=self._status_value(value))
        return value

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _status_value(value: Any) -> float | None:
        if isinstance(value, float):
            return value
        if isinstance(value, FiatRates):
            return value.usd
        if isinstance(value, BankRates) and value.usd:
            return value.usd.sell
        return None

    def _record(
        self, provider: Provider, now: float, *, ok: bool, latency_ms: int,
        value: float | None = None, error: str | None = None,
    ) -> None:
        prev = self.statuses.get(provider.key)
        self.statuses[provider.key] = SourceStatus(
            key=provider.key, name=provider.name, group=provider.group, ok=ok, checked_at=now,
            last_ok_at=now if ok else (prev.last_ok_at if prev else None),
            latency_ms=latency_ms, value=value, error=error,
            failures=0 if ok else (prev.failures + 1 if prev else 1),
        )
        # В лог попадают только смены состояния — не по строке на каждый опрос.
        if not ok and (prev is None or prev.ok):
            log.warning("источник «%s» недоступен: %s", provider.name, error)
        elif ok and prev is not None and not prev.ok:
            log.info("источник «%s» снова работает", provider.name)
