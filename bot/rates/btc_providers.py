"""Источники цены BTC/USD. Все — публичные API без ключей.

Цена берётся с нескольких бирж/агрегаторов и сводится к медиане (см. aggregate.py), поэтому
недоступность отдельных источников (в том числе из-за блокировок в вашей сети) не мешает работе.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .http import Fetcher, ParseError, fetch_with_mirrors


def _price(value: Any) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise ParseError(f"не число: {value!r}") from None
    if not math.isfinite(price) or price <= 0:
        raise ParseError(f"некорректная цена: {value!r}")
    return price


# --------------------------------------------------------------------------- #
# Разбор ответов (чистые функции — легко тестировать на готовых JSON)
# --------------------------------------------------------------------------- #
def parse_binance_like(d: Any) -> float:  # Binance, MEXC: {"symbol": "BTCUSDT", "price": "83418.79"}
    return _price(d["price"])


def parse_coinbase(d: Any) -> float:
    return _price(d["data"]["amount"])


def parse_kraken(d: Any) -> float:
    if d.get("error"):
        raise ParseError(str(d["error"])[:80])
    ticker = next(iter(d["result"].values()), None)
    if ticker is None:
        raise ParseError("пустой result")
    return _price(ticker["c"][0])  # c = [цена последней сделки, объём]


def parse_bitstamp(d: Any) -> float:
    return _price(d["last"])


def parse_bitfinex(d: Any) -> float:
    if not isinstance(d, list) or len(d) < 7:  # ошибки приходят как ["error", 10020, "..."]
        raise ParseError("ожидался массив тикера")
    return _price(d[6])  # LAST_PRICE


def parse_okx(d: Any) -> float:
    if str(d.get("code")) != "0":
        raise ParseError(str(d.get("msg"))[:80])
    return _price(d["data"][0]["last"])


def parse_kucoin(d: Any) -> float:
    if str(d.get("code")) != "200000":
        raise ParseError(str(d.get("msg"))[:80])
    return _price(d["data"]["price"])


def parse_bybit(d: Any) -> float:
    if d.get("retCode") != 0:
        raise ParseError(str(d.get("retMsg"))[:80])
    return _price(d["result"]["list"][0]["lastPrice"])


def parse_gate(d: Any) -> float:
    return _price(d[0]["last"])


def parse_htx(d: Any) -> float:
    if d.get("status") != "ok":
        raise ParseError(str(d.get("err-msg") or d.get("status"))[:80])
    return _price(d["tick"]["close"])


def parse_coingecko(d: Any) -> float:
    return _price(d["bitcoin"]["usd"])


def parse_blockchain(d: Any) -> float:
    return _price(d["USD"]["last"])


# --------------------------------------------------------------------------- #
# Провайдеры
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BtcProvider:
    key: str
    name: str
    urls: tuple[str, ...]  # основной адрес и зеркала
    parse: Callable[[Any], float]
    params: Mapping[str, str] | None = None
    group: str = "btc"

    async def fetch(self, fetcher: Fetcher) -> float:
        return await fetch_with_mirrors(fetcher, self.urls, self.parse, self.params)


DEFAULT_BTC_PROVIDERS: tuple[BtcProvider, ...] = (
    BtcProvider(
        "binance", "Binance",
        ("https://api.binance.com/api/v3/ticker/price", "https://data-api.binance.vision/api/v3/ticker/price"),
        parse_binance_like, {"symbol": "BTCUSDT"},
    ),
    BtcProvider(
        "coinbase", "Coinbase", ("https://api.coinbase.com/v2/prices/BTC-USD/spot",), parse_coinbase,
    ),
    BtcProvider(
        "kraken", "Kraken", ("https://api.kraken.com/0/public/Ticker",), parse_kraken, {"pair": "XBTUSD"},
    ),
    BtcProvider(
        "bitstamp", "Bitstamp", ("https://www.bitstamp.net/api/v2/ticker/btcusd/",), parse_bitstamp,
    ),
    BtcProvider(
        "bitfinex", "Bitfinex", ("https://api-pub.bitfinex.com/v2/ticker/tBTCUSD",), parse_bitfinex,
    ),
    BtcProvider(
        "okx", "OKX", ("https://www.okx.com/api/v5/market/ticker",), parse_okx, {"instId": "BTC-USDT"},
    ),
    BtcProvider(
        "kucoin", "KuCoin", ("https://api.kucoin.com/api/v1/market/orderbook/level1",), parse_kucoin,
        {"symbol": "BTC-USDT"},
    ),
    BtcProvider(
        "bybit", "Bybit", ("https://api.bybit.com/v5/market/tickers",), parse_bybit,
        {"category": "spot", "symbol": "BTCUSDT"},
    ),
    BtcProvider(
        "gate", "Gate.io", ("https://api.gateio.ws/api/v4/spot/tickers",), parse_gate,
        {"currency_pair": "BTC_USDT"},
    ),
    BtcProvider(
        "mexc", "MEXC", ("https://api.mexc.com/api/v3/ticker/price",), parse_binance_like, {"symbol": "BTCUSDT"},
    ),
    BtcProvider(
        "htx", "HTX", ("https://api.huobi.pro/market/detail/merged",), parse_htx, {"symbol": "btcusdt"},
    ),
    BtcProvider(
        "coingecko", "CoinGecko", ("https://api.coingecko.com/api/v3/simple/price",), parse_coingecko,
        {"ids": "bitcoin", "vs_currencies": "usd"},
    ),
    BtcProvider(
        "blockchain", "Blockchain.com", ("https://blockchain.info/ticker",), parse_blockchain,
    ),
)
