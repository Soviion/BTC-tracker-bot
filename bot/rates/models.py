"""Модели данных для курсов валют и BTC."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class BtcQuote:
    """Итоговая цена BTC в USD — медиана по нескольким источникам."""

    price: float
    ts: float
    sources_used: int
    sources_total: int
    spread_pct: float
    prices: dict[str, float] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class FiatRates:
    """Курсы к BYN: сколько белорусских рублей за 1 единицу валюты."""

    usd: float | None
    eur: float | None
    rub: float | None  # за 1 RUB (в сообщениях показываем за 100)
    rates_date: date | None
    source: str
    official: bool
    ts: float


@dataclass(frozen=True)
class BankRate:
    buy: float  # банк покупает валюту у клиента
    sell: float  # банк продаёт валюту клиенту


@dataclass(frozen=True)
class BankRates:
    usd: BankRate | None
    eur: BankRate | None
    rub: BankRate | None  # за 1 RUB
    source: str
    ts: float


@dataclass(frozen=True)
class Snapshot:
    """Что бот знает о курсах прямо сейчас (каждая часть со своим временем обновления)."""

    btc: BtcQuote | None = None
    fiat: FiatRates | None = None
    bank: BankRates | None = None

    @property
    def usd_byn(self) -> float | None:
        return self.fiat.usd if self.fiat else None

    @property
    def btc_byn(self) -> float | None:
        if self.btc and self.usd_byn:
            return self.btc.price * self.usd_byn
        return None


@dataclass(frozen=True)
class SourceStatus:
    """Состояние одного источника данных — для экрана «Статус»."""

    key: str
    name: str
    group: str  # "btc" | "fiat"
    ok: bool
    checked_at: float
    last_ok_at: float | None
    latency_ms: int | None
    value: float | None
    error: str | None
    failures: int
