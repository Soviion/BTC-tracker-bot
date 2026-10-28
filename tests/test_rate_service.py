"""RateService: агрегация, выбросы, защита от скачков, отказ источников, переключение НБРБ → резерв."""

from __future__ import annotations

import asyncio

import pytest

from bot.rates.fiat_providers import DEFAULT_FIAT_PROVIDERS
from bot.rates.service import RateService
from tests.samples import BTC_KEYS, FakeFetcher

T0 = 1_800_000_000.0
OPEN_ER_URL = next(p.urls[0] for p in DEFAULT_FIAT_PROVIDERS if p.key == "open_er")
NBRB_URL = next(p.urls[0] for p in DEFAULT_FIAT_PROVIDERS if p.key == "nbrb")


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(**kwargs) -> tuple[RateService, FakeFetcher, Clock]:
    fetcher, clock = FakeFetcher(), Clock()
    return RateService(fetcher, clock=clock, **kwargs), fetcher, clock


# --------------------------------------------------------------------------- #
# BTC
# --------------------------------------------------------------------------- #
async def test_poll_aggregates_all_sources() -> None:
    service, _, _ = make()
    snap = await service.poll()

    assert snap.btc.price == pytest.approx(83000.0)
    assert (snap.btc.sources_used, snap.btc.sources_total) == (13, 13)
    assert snap.fiat.usd == pytest.approx(3.0739) and snap.fiat.official is True
    assert snap.bank.usd.sell == pytest.approx(3.07)
    assert snap.btc_byn == pytest.approx(83000.0 * 3.0739)
    assert all(service.statuses[key].ok for key in BTC_KEYS)


async def test_outlier_source_is_rejected() -> None:
    service, fetcher, _ = make()
    fetcher.btc_price["binance"] = 120_000.0  # один источник «сошёл с ума»

    snap = await service.poll()

    assert snap.btc.price == pytest.approx(83000.0)
    assert snap.btc.sources_used == 12 and "binance" not in snap.btc.prices


async def test_failed_sources_are_reported_and_recover() -> None:
    service, fetcher, clock = make()
    fetcher.failing = {"okx", "kraken"}

    snap = await service.poll()
    assert snap.btc.sources_used == 11
    assert service.statuses["okx"].ok is False
    assert service.statuses["okx"].error == "HTTP 503"
    assert service.statuses["okx"].failures == 1

    clock.t += 60
    await service.poll()
    assert service.statuses["okx"].failures == 2  # счётчик сбоев подряд растёт

    fetcher.failing.clear()
    clock.t += 60
    snap = await service.poll()
    assert snap.btc.sources_used == 13
    status = service.statuses["okx"]
    assert status.ok is True and status.failures == 0 and status.last_ok_at == clock.t


async def test_all_sources_down_keeps_old_quote_until_it_goes_stale() -> None:
    service, fetcher, clock = make()
    await service.poll()
    fetcher.failing = set(BTC_KEYS)

    clock.t += 100
    snap = await service.poll()
    assert snap.btc.ts == T0  # новых данных нет — старая котировка остаётся как есть
    assert service.fresh_btc() is not None  # 100 с < stale_after (180 с)

    clock.t += 200
    await service.poll()
    assert service.fresh_btc() is None  # устарела: алерты по ней срабатывать не должны
    assert service.snapshot().btc is not None  # но показать «последнее известное» можно


async def test_small_moves_apply_immediately() -> None:
    service, fetcher, clock = make()
    await service.poll()
    fetcher.set_price(84_000.0)  # +1.2%
    clock.t += 60
    await service.poll()
    assert service.btc.price == pytest.approx(84_000.0)


async def test_sudden_jump_needs_confirmation_by_next_poll() -> None:
    service, fetcher, clock = make()
    await service.poll()

    fetcher.set_price(60_000.0)  # −28% за минуту — подозрительно
    clock.t += 60
    await service.poll()
    assert service.btc.price == pytest.approx(83_000.0)  # пока держим прежнюю цену

    fetcher.set_price(60_100.0)  # следующий опрос подтвердил движение
    clock.t += 60
    await service.poll()
    assert service.btc.price == pytest.approx(60_100.0)


async def test_unconfirmed_spike_is_dropped() -> None:
    service, fetcher, clock = make()
    await service.poll()

    fetcher.set_price(60_000.0)
    clock.t += 60
    await service.poll()
    fetcher.set_price(83_100.0)  # «скачок» не повторился — это был мусор
    clock.t += 60
    await service.poll()
    assert service.btc.price == pytest.approx(83_100.0)


async def test_jump_is_accepted_at_once_when_previous_quote_is_old() -> None:
    service, fetcher, clock = make()
    await service.poll()
    fetcher.set_price(60_000.0)
    clock.t += 700  # прошлая цена старше 10 минут — сравнивать «за минуту» уже не с чем
    await service.poll()
    assert service.btc.price == pytest.approx(60_000.0)


# --------------------------------------------------------------------------- #
# Курсы BYN
# --------------------------------------------------------------------------- #
async def test_official_outage_within_grace_keeps_old_official_rate() -> None:
    service, fetcher, clock = make()
    await service.poll()
    fetcher.failing = {"nbrb"}

    clock.t += 300  # меньше official_grace (15 минут)
    snap = await service.poll()

    assert snap.fiat.official is True and snap.fiat.usd == pytest.approx(3.0739)
    assert OPEN_ER_URL not in fetcher.calls  # до резервных источников дело не дошло


async def test_falls_back_to_international_after_grace_and_returns_to_official() -> None:
    service, fetcher, clock = make()
    await service.poll()
    fetcher.failing = {"nbrb"}

    clock.t += 1000  # дольше official_grace
    snap = await service.poll()
    assert snap.fiat.official is False and snap.fiat.source.startswith("Open ER")

    fetcher.failing.clear()
    clock.t += 60
    snap = await service.poll()
    assert snap.fiat.official is True and snap.fiat.source == "НБРБ"


async def test_fallback_api_is_not_hammered() -> None:
    service, fetcher, clock = make()
    fetcher.failing = {"nbrb"}  # НБРБ недоступен с самого старта

    await service.poll()
    assert fetcher.calls.count(OPEN_ER_URL) == 1

    for _ in range(5):  # следующие 5 минут — берём из кэша, а не стучимся в лимитированный API
        clock.t += 60
        snap = await service.poll()
        assert snap.fiat.official is False
    assert fetcher.calls.count(OPEN_ER_URL) == 1

    clock.t += 301  # кэш старше fallback_interval (10 минут) — можно обновить
    await service.poll()
    assert fetcher.calls.count(OPEN_ER_URL) == 2


async def test_no_fiat_sources_at_all_does_not_break_btc() -> None:
    service, fetcher, _ = make()
    fetcher.failing = {"nbrb", "belarusbank", "open_er", "currency_api"}

    snap = await service.poll()

    assert snap.fiat is None and snap.bank is None
    assert snap.btc is not None and snap.btc_byn is None  # цена в BYN без курса не считается


async def test_bank_rates_are_independent_of_official() -> None:
    service, fetcher, _ = make()
    fetcher.failing = {"belarusbank"}

    snap = await service.poll()

    assert snap.bank is None
    assert snap.fiat.official is True


async def test_garbage_responses_never_break_poll() -> None:
    service, fetcher, _ = make()
    fetcher.fiat["nbrb"] = "<html>bad gateway</html>"
    fetcher.fiat["belarusbank"] = None

    snap = await service.poll()  # не должно бросать исключений

    assert snap.btc is not None
    assert service.statuses["nbrb"].ok is False and "USD" in service.statuses["nbrb"].error
    assert service.statuses["belarusbank"].ok is False


# --------------------------------------------------------------------------- #
# Обзор источников и кнопка «Обновить»
# --------------------------------------------------------------------------- #
async def test_source_overview_lists_every_provider_in_order() -> None:
    service, _, _ = make()
    overview = service.source_overview()
    assert len(overview) == 13 + 4
    assert [p.key for p, _ in overview[:13]] == list(BTC_KEYS)
    assert all(status is None for _, status in overview)  # до первого опроса статусов нет

    await service.poll()
    statuses = dict((p.key, st) for p, st in service.source_overview())
    assert statuses["binance"].ok and statuses["nbrb"].ok
    assert statuses["open_er"] is None  # резервные не трогали, пока НБРБ жив


async def test_refresh_if_stale_polls_only_when_data_is_old() -> None:
    service, fetcher, clock = make()
    await service.poll()
    calls_after_first = len(fetcher.calls)

    clock.t += 5
    await service.refresh_if_stale(20.0)
    assert len(fetcher.calls) == calls_after_first  # данные свежие — лишних запросов нет

    clock.t += 30
    await service.refresh_if_stale(20.0)
    assert len(fetcher.calls) > calls_after_first


async def test_button_spam_triggers_single_poll() -> None:
    service, fetcher, _ = make()
    await asyncio.gather(*(service.refresh_if_stale(20.0) for _ in range(8)))  # 8 нажатий «Обновить» разом

    probe_fetcher = FakeFetcher()  # эталон: сколько запросов делает ОДИН опрос
    await RateService(probe_fetcher, clock=Clock()).poll()
    assert len(fetcher.calls) == len(probe_fetcher.calls)
