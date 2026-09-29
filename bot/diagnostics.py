"""Автономная проверка источников курсов — без Telegram и без БД.

Запуск с сервера, где будет жить бот:  python -m bot --check-apis
Полезно перед первым стартом: НБРБ и Belarusbank обычно отвечают только с белорусских IP.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from .rates.btc_providers import DEFAULT_BTC_PROVIDERS
from .rates.fiat_providers import DEFAULT_FIAT_PROVIDERS
from .rates.http import JsonFetcher, describe_error

PROBE_TIMEOUT = 12.0


async def _probe(factory: Callable[[], Awaitable[Any]]) -> tuple[bool, str]:
    started = time.monotonic()
    try:
        value = await asyncio.wait_for(factory(), timeout=PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — цель диагностики: показать причину, а не упасть
        return False, describe_error(exc)
    ms = int((time.monotonic() - started) * 1000)
    shown = value if isinstance(value, float) else getattr(value, "usd", None)
    tail = f" · {shown:,.2f}".replace(",", " ") if isinstance(shown, float) else ""
    return True, f"{ms} мс{tail}"


async def check_apis() -> bool:
    """Печатает статус каждого источника. True — базовый минимум (BTC + курс BYN) доступен."""
    fetcher = JsonFetcher(timeout=PROBE_TIMEOUT)
    print("Проверяю источники курсов (до ~15 секунд)…\n")

    print("₿ Bitcoin:")
    btc_ok = 0
    for p in DEFAULT_BTC_PROVIDERS:
        ok, info = await _probe(lambda p=p: p.fetch(fetcher))
        btc_ok += ok
        print(f"  {'✅' if ok else '❌'} {p.name:<14} {info}")

    print("\n🏦 Курсы BYN:")
    fiat_ok: dict[str, bool] = {}
    now = time.time()
    for p in DEFAULT_FIAT_PROVIDERS:
        ok, info = await _probe(lambda p=p: p.fetch(fetcher, now))
        fiat_ok[p.key] = ok
        print(f"  {'✅' if ok else '❌'} {p.name:<28} {info}")

    await fetcher.close()

    print()
    if btc_ok == 0:
        print("❌ Ни один источник BTC не отвечает — проверьте интернет на сервере.")
    elif btc_ok < 3:
        print(f"⚠️ Отвечают только {btc_ok} источник(ов) BTC — бот заработает, но данные будут не такими надёжными.")
    else:
        print(f"✅ BTC: доступно {btc_ok}/{len(DEFAULT_BTC_PROVIDERS)} источников.")

    fiat_basic_ok = fiat_ok.get("nbrb") or fiat_ok.get("open_er") or fiat_ok.get("currency_api")
    if fiat_ok.get("nbrb"):
        print("✅ НБРБ (официальный курс BYN) доступен.")
    elif fiat_basic_ok:
        print("⚠️ НБРБ недоступен, но резервный международный источник курса BYN работает.")
    else:
        print("❌ Ни НБРБ, ни резервные источники курса BYN не отвечают.")
    if not fiat_ok.get("belarusbank"):
        print("ℹ️ Belarusbank недоступен — не критично, просто не будут показываться курсы покупки/продажи банка.")

    return btc_ok > 0 and bool(fiat_basic_ok)
