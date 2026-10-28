"""Образцы ответов API источников курсов и фальшивый HTTP-клиент для тестов.

Структура образцов повторяет настоящие ответы бирж/НБРБ/Belarusbank (снято с живых API), поэтому тесты
ловят «дрейф» формата, а не только арифметику. Значения курсов — правдоподобные, но не боевые.
"""

from __future__ import annotations

from typing import Any

from bot.rates.btc_providers import DEFAULT_BTC_PROVIDERS
from bot.rates.fiat_providers import DEFAULT_FIAT_PROVIDERS
from bot.rates.http import FetchError

BTC_KEYS = tuple(p.key for p in DEFAULT_BTC_PROVIDERS)


def btc_payload(key: str, price: float) -> Any:
    """Ответ источника `key` с ценой `price` — в точности той формы, что отдаёт настоящий API."""
    return {
        "binance": {"symbol": "BTCUSDT", "price": f"{price:.8f}"},
        "mexc": {"symbol": "BTCUSDT", "price": f"{price}"},
        "coinbase": {"data": {"amount": f"{price}", "base": "BTC", "currency": "USD"}},
        "kraken": {"error": [], "result": {"XXBTZUSD": {
            "a": [f"{price:.5f}", "1", "1.000"], "b": [f"{price:.5f}", "1", "1.000"],
            "c": [f"{price:.5f}", "0.00050000"], "v": ["1812.9", "3362.3"],
        }}},
        "bitstamp": {"timestamp": "1790345329", "last": f"{price:.2f}", "bid": f"{price:.2f}", "ask": f"{price:.2f}"},
        "bitfinex": [price, 4.2, price, 2.7, -1511, -0.0178, price, 1297.1, price, price, 1358182043000],
        "okx": {"code": "0", "msg": "", "data": [{"instType": "SPOT", "instId": "BTC-USDT", "last": f"{price}"}]},
        "kucoin": {"code": "200000", "data": {"time": 1790345332149, "sequence": "37732342883", "price": f"{price}"}},
        "bybit": {"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": [
            {"symbol": "BTCUSDT", "lastPrice": f"{price}", "prevPrice24h": "84356.3"},
        ]}},
        "gate": [{"currency_pair": "BTC_USDT", "last": f"{price}", "lowest_ask": f"{price}"}],
        "htx": {"ch": "market.btcusdt.detail.merged", "status": "ok", "ts": 1790345343310,
                "tick": {"id": 387699518021, "open": 84228.92, "close": price, "low": 83167.01}},
        "coingecko": {"bitcoin": {"usd": price}},
        "blockchain": {"ARS": {"15m": 1.2e8, "last": 1.2e8, "symbol": "ARS"},
                       "USD": {"15m": price, "last": price, "buy": price, "sell": price, "symbol": "USD"}},
    }[key]


NBRB_SAMPLE: list[dict[str, Any]] = [
    {"Cur_ID": 431, "Date": "2026-10-07T00:00:00", "Cur_Abbreviation": "USD", "Cur_Scale": 1,
     "Cur_Name": "Доллар США", "Cur_OfficialRate": 3.0739},
    {"Cur_ID": 451, "Date": "2026-10-07T00:00:00", "Cur_Abbreviation": "EUR", "Cur_Scale": 1,
     "Cur_Name": "Евро", "Cur_OfficialRate": 3.456},
    {"Cur_ID": 456, "Date": "2026-10-07T00:00:00", "Cur_Abbreviation": "RUB", "Cur_Scale": 100,
     "Cur_Name": "Российских рублей", "Cur_OfficialRate": 3.587},
    {"Cur_ID": 452, "Date": "2026-10-07T00:00:00", "Cur_Abbreviation": "PLN", "Cur_Scale": 10,
     "Cur_Name": "Злотых", "Cur_OfficialRate": 8.1234},  # посторонняя валюта — должна игнорироваться
]


def belarusbank_branch(**overrides: str) -> dict[str, Any]:
    row = {
        "filial_id": "100", "name": "Минск", "kurs_date_time": "2026-10-07 09:00:00",
        "USD_in": "3.0300", "USD_out": "3.0700", "EUR_in": "3.3800", "EUR_out": "3.4450",
        "RUB_in": "3.1500", "RUB_out": "3.5900",  # у банка рубль котируется за 100 RUB
        "USD_EUR_in": "0.884", "USD_EUR_out": "1.106", "USD_RUB_in": "84.5", "USD_RUB_out": "0.0103",
    }
    row.update(overrides)
    return row


BELARUSBANK_SAMPLE: list[dict[str, Any]] = [belarusbank_branch(), belarusbank_branch(), belarusbank_branch()]

OPEN_ER_SAMPLE: dict[str, Any] = {
    "result": "success", "provider": "https://www.exchangerate-api.com",
    "time_last_update_utc": "Wed, 07 Oct 2026 00:02:31 +0000", "base_code": "USD",
    "rates": {"USD": 1, "BYN": 3.070244, "EUR": 0.8887, "RUB": 84.95},
}

CURRENCY_API_SAMPLE: dict[str, Any] = {
    "date": "2026-10-06", "usd": {"byn": 3.05072461, "eur": 0.8892, "rub": 84.84},
}


class FakeFetcher:
    """Подменяет JsonFetcher: отвечает заготовками по URL, остальное — «HTTP 404».

    * `btc_price[key]` — цена, которую «отдаёт» источник (можно менять между опросами);
    * `failing` — ключи источников, которые «не отвечают» (FetchError);
    * `fiat[key]` — ответ фиатного источника (можно подменить целиком, в т.ч. мусором).
    """

    def __init__(self, price: float = 83000.0) -> None:
        self.btc_price: dict[str, float] = {key: price for key in BTC_KEYS}
        self.failing: set[str] = set()
        self.fiat: dict[str, Any] = {
            "nbrb": NBRB_SAMPLE, "belarusbank": BELARUSBANK_SAMPLE,
            "open_er": OPEN_ER_SAMPLE, "currency_api": CURRENCY_API_SAMPLE,
        }
        self.calls: list[str] = []

    def set_price(self, price: float) -> None:
        for key in self.btc_price:
            self.btc_price[key] = price

    async def get_json(self, url: str, *, params: Any = None) -> Any:
        self.calls.append(url)
        for provider in (*DEFAULT_BTC_PROVIDERS, *DEFAULT_FIAT_PROVIDERS):
            if url in provider.urls:
                if provider.key in self.failing:
                    raise FetchError("HTTP 503")
                if provider.key in BTC_KEYS:
                    return btc_payload(provider.key, self.btc_price[provider.key])
                return self.fiat[provider.key]
        raise FetchError("HTTP 404")

    async def close(self) -> None:
        return None
