"""Источники курсов BYN.

* НБРБ — официальный курс Национального банка Беларуси (основной).
* Belarusbank — реальные курсы покупки/продажи в отделениях банка.
* Open ER API и currency-api — международные курсы; используются только как запасной вариант,
  если НБРБ недоступен (они опрашиваются редко, чтобы не нарушать лимиты бесплатных API).

Белорусские сервисы (api.nbrb.by, belarusbank.by) обычно доступны только с белорусских IP —
поэтому именно с сервера в Беларуси они и должны работать. Проверить: `python -m bot --check-apis`.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

from .http import Fetcher, ParseError, fetch_with_mirrors
from .models import BankRate, BankRates, FiatRates


def _positive(value: Any) -> float | None:
    try:
        x = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# НБРБ: https://api.nbrb.by/exrates/rates?periodicity=0
#   [{"Cur_ID":431,"Date":"2026-09-25T00:00:00","Cur_Abbreviation":"USD","Cur_Scale":1,
#     "Cur_Name":"Доллар США","Cur_OfficialRate":3.0351}, ...]
# --------------------------------------------------------------------------- #
def parse_nbrb(data: Any, now: float) -> FiatRates:
    rows = data if isinstance(data, list) else [data]
    per_unit: dict[str, float] = {}
    rates_date: date | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("Cur_Abbreviation", "")).upper()
        if code not in ("USD", "EUR", "RUB"):
            continue
        rate, scale = _positive(row.get("Cur_OfficialRate")), _positive(row.get("Cur_Scale", 1))
        if rate is None or scale is None:
            continue
        per_unit[code] = rate / scale  # RUB приходит за 100 — приводим к «за 1»
        rates_date = rates_date or _iso_date(row.get("Date"))
    if "USD" not in per_unit:
        raise ParseError("в ответе НБРБ нет курса USD")
    return FiatRates(
        usd=per_unit["USD"], eur=per_unit.get("EUR"), rub=per_unit.get("RUB"),
        rates_date=rates_date, source="НБРБ", official=True, ts=now,
    )


# --------------------------------------------------------------------------- #
# Belarusbank: https://belarusbank.by/api/kursExchange?city=Минск
#   список отделений: {"USD_in": "2.98", "USD_out": "3.05", "EUR_in": ..., "RUB_in": ..., ...}
#   *_in — банк покупает валюту, *_out — продаёт. Берём медиану по отделениям.
# --------------------------------------------------------------------------- #
def _median_field(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [x for r in rows if (x := _positive(r.get(key))) is not None]
    return statistics.median(values) if values else None


def parse_belarusbank(data: Any, now: float) -> BankRates:
    if not isinstance(data, list) or not data:
        raise ParseError("ожидался непустой список отделений")
    rows = [r for r in data if isinstance(r, dict)]

    def pair(code: str) -> BankRate | None:
        a, b = _median_field(rows, f"{code}_in"), _median_field(rows, f"{code}_out")
        if a is None or b is None:
            return None
        if code == "RUB" and min(a, b) > 0.5:  # банк котирует за 100 RUB — приводим к «за 1»
            a, b = a / 100, b / 100
        return BankRate(buy=min(a, b), sell=max(a, b))  # покупка не бывает дороже продажи

    usd = pair("USD")
    if usd is None:
        raise ParseError(f"нет курса USD; поля: {sorted(rows[0])[:10] if rows else '—'}")
    return BankRates(usd=usd, eur=pair("EUR"), rub=pair("RUB"), source="Belarusbank", ts=now)


# --------------------------------------------------------------------------- #
# Международные резервные источники (курс BYN считают сами)
# --------------------------------------------------------------------------- #
def parse_open_er(data: Any, now: float) -> FiatRates:
    if data.get("result") != "success":
        raise ParseError(str(data.get("error-type", "result != success")))
    rates = data["rates"]
    byn, eur, rub = _positive(rates.get("BYN")), _positive(rates.get("EUR")), _positive(rates.get("RUB"))
    if byn is None:
        raise ParseError("в ответе нет BYN")
    try:
        rates_date: date | None = parsedate_to_datetime(data["time_last_update_utc"]).date()
    except (KeyError, TypeError, ValueError):
        rates_date = None
    return FiatRates(
        usd=byn, eur=byn / eur if eur else None, rub=byn / rub if rub else None,
        rates_date=rates_date, source="Open ER API (межд.)", official=False, ts=now,
    )


def parse_currency_api(data: Any, now: float) -> FiatRates:
    usd = data["usd"]
    byn, eur, rub = _positive(usd.get("byn")), _positive(usd.get("eur")), _positive(usd.get("rub"))
    if byn is None:
        raise ParseError("в ответе нет byn")
    return FiatRates(
        usd=byn, eur=byn / eur if eur else None, rub=byn / rub if rub else None,
        rates_date=_iso_date(data.get("date")), source="currency-api (межд.)", official=False, ts=now,
    )


# --------------------------------------------------------------------------- #
# Провайдеры
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FiatProvider:
    key: str
    name: str
    kind: str  # "official" | "bank" | "fallback"
    urls: tuple[str, ...]
    parse: Callable[[Any, float], Any]
    params: Mapping[str, str] | None = None
    group: str = "fiat"

    async def fetch(self, fetcher: Fetcher, now: float) -> Any:
        return await fetch_with_mirrors(fetcher, self.urls, lambda payload: self.parse(payload, now), self.params)


DEFAULT_FIAT_PROVIDERS: tuple[FiatProvider, ...] = (
    FiatProvider(
        "nbrb", "НБРБ (официальный)", "official",
        ("https://api.nbrb.by/exrates/rates", "https://www.nbrb.by/api/exrates/rates"),
        parse_nbrb, {"periodicity": "0"},
    ),
    FiatProvider(
        "belarusbank", "Belarusbank (курсы банка)", "bank",
        ("https://belarusbank.by/api/kursExchange", "https://www.belarusbank.by/api/kursExchange"),
        parse_belarusbank, {"city": "Минск"},
    ),
    FiatProvider(
        "open_er", "Open ER API (резерв)", "fallback",
        ("https://open.er-api.com/v6/latest/USD",), parse_open_er,
    ),
    FiatProvider(
        "currency_api", "currency-api (резерв)", "fallback",
        (
            "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
            "https://latest.currency-api.pages.dev/v1/currencies/usd.json",
        ),
        parse_currency_api,
    ),
)
