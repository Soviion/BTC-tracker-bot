"""Разбор ответов всех источников курсов на образцах реальной формы + логика зеркал и описания ошибок."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from bot.rates.btc_providers import (
    DEFAULT_BTC_PROVIDERS,
    parse_bitfinex,
    parse_bybit,
    parse_coinbase,
    parse_htx,
    parse_kraken,
    parse_kucoin,
    parse_okx,
)
from bot.rates.fiat_providers import (
    DEFAULT_FIAT_PROVIDERS,
    parse_belarusbank,
    parse_currency_api,
    parse_nbrb,
    parse_open_er,
)
from bot.rates.http import FetchError, ParseError, describe_error, fetch_with_mirrors
from bot.rates.models import BankRates, FiatRates
from tests.samples import (
    BELARUSBANK_SAMPLE,
    CURRENCY_API_SAMPLE,
    NBRB_SAMPLE,
    OPEN_ER_SAMPLE,
    FakeFetcher,
    belarusbank_branch,
    btc_payload,
)

NOW = 1_800_000_000.0


# --------------------------------------------------------------------------- #
# BTC: все 13 источников
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider", DEFAULT_BTC_PROVIDERS, ids=lambda p: p.key)
def test_every_btc_provider_parses_realistic_payload(provider) -> None:
    assert provider.parse(btc_payload(provider.key, 83123.45)) == pytest.approx(83123.45)


def test_btc_provider_keys_are_unique_and_have_urls() -> None:
    keys = [p.key for p in DEFAULT_BTC_PROVIDERS]
    assert len(keys) == len(set(keys)) == 13
    assert all(p.urls and p.urls[0].startswith("https://") for p in DEFAULT_BTC_PROVIDERS)


@pytest.mark.parametrize(
    "parser, payload",
    [
        (parse_kraken, {"error": ["EQuery:Unknown asset pair"], "result": {}}),
        (parse_kraken, {"error": [], "result": {}}),
        (parse_bitfinex, ["error", 10020, "symbol: invalid"]),
        (parse_bitfinex, {"unexpected": "object"}),
        (parse_okx, {"code": "51001", "msg": "Instrument ID doesn't exist", "data": []}),
        (parse_kucoin, {"code": "400100", "msg": "bad symbol"}),
        (parse_bybit, {"retCode": 10001, "retMsg": "params error"}),
        (parse_htx, {"status": "error", "err-msg": "invalid symbol"}),
    ],
)
def test_btc_parsers_reject_error_payloads(parser, payload) -> None:
    with pytest.raises(ParseError):
        parser(payload)


@pytest.mark.parametrize("bad", ["0", "-5", "nan", "inf", "abc", None, ""])
def test_btc_parsers_reject_nonsense_prices(bad) -> None:
    with pytest.raises(ParseError):
        parse_coinbase({"data": {"amount": bad}})


def test_btc_parsers_raise_on_structure_drift() -> None:
    # изменившаяся структура ответа — это KeyError/IndexError/TypeError, а не молчаливая «цена 0»;
    # RateService ловит любые исключения источника и показывает причину в статусе
    with pytest.raises((KeyError, IndexError, TypeError)):
        parse_coinbase({"data": {}})
    with pytest.raises((KeyError, IndexError, TypeError)):
        parse_okx({"code": "0", "data": []})


# --------------------------------------------------------------------------- #
# Зеркала и описание ошибок
# --------------------------------------------------------------------------- #
class SequenceFetcher:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def get_json(self, url: str, *, params=None):
        self.calls.append(url)
        result = self.results[url]
        if isinstance(result, BaseException):
            raise result
        return result


async def test_mirrors_first_failure_falls_back_to_second() -> None:
    fetcher = SequenceFetcher({"u1": FetchError("HTTP 503"), "u2": {"v": 5}})
    assert await fetch_with_mirrors(fetcher, ("u1", "u2"), lambda d: d["v"]) == 5
    assert fetcher.calls == ["u1", "u2"]


async def test_mirrors_stop_at_first_success() -> None:
    fetcher = SequenceFetcher({"u1": {"v": 1}, "u2": {"v": 2}})
    assert await fetch_with_mirrors(fetcher, ("u1", "u2"), lambda d: d["v"]) == 1
    assert fetcher.calls == ["u1"]


async def test_mirrors_parse_error_also_triggers_fallback() -> None:
    fetcher = SequenceFetcher({"u1": {"garbage": True}, "u2": {"v": 7}})
    assert await fetch_with_mirrors(fetcher, ("u1", "u2"), lambda d: d["v"]) == 7


async def test_mirrors_all_failed_raises_last_error() -> None:
    fetcher = SequenceFetcher({"u1": FetchError("HTTP 500"), "u2": FetchError("HTTP 404")})
    with pytest.raises(FetchError, match="404"):
        await fetch_with_mirrors(fetcher, ("u1", "u2"), lambda d: d)


async def test_mirrors_do_not_swallow_cancellation() -> None:
    fetcher = SequenceFetcher({"u1": asyncio.CancelledError(), "u2": {"v": 1}})
    with pytest.raises(asyncio.CancelledError):
        await fetch_with_mirrors(fetcher, ("u1", "u2"), lambda d: d["v"])
    assert fetcher.calls == ["u1"]  # отмена не должна «переключаться» на зеркало


def test_describe_error_is_human_readable() -> None:
    assert describe_error(FetchError("HTTP 403")) == "HTTP 403"
    assert describe_error(TimeoutError()) == "таймаут"
    assert describe_error(ParseError("не число")) == "неожиданный ответ: не число"
    assert describe_error(KeyError("data")).startswith("неожиданный формат")


# --------------------------------------------------------------------------- #
# НБРБ
# --------------------------------------------------------------------------- #
def test_nbrb_parses_real_shape_and_scales_rub() -> None:
    rates = parse_nbrb(NBRB_SAMPLE, NOW)
    assert isinstance(rates, FiatRates) and rates.official is True and rates.source == "НБРБ"
    assert rates.usd == pytest.approx(3.0739)
    assert rates.eur == pytest.approx(3.456)
    assert rates.rub == pytest.approx(3.587 / 100)  # НБРБ даёт рубль за 100 — приводим к «за 1»
    assert rates.rates_date == date(2026, 10, 7)
    assert rates.ts == NOW


def test_nbrb_accepts_single_object_and_ignores_junk_rows() -> None:
    single = parse_nbrb(NBRB_SAMPLE[0], NOW)
    assert single.usd == pytest.approx(3.0739) and single.eur is None
    with_junk = parse_nbrb(["junk", None, 5, *NBRB_SAMPLE], NOW)
    assert with_junk.usd == pytest.approx(3.0739)


def test_nbrb_requires_usd() -> None:
    with pytest.raises(ParseError, match="USD"):
        parse_nbrb([row for row in NBRB_SAMPLE if row["Cur_Abbreviation"] != "USD"], NOW)
    with pytest.raises(ParseError):
        parse_nbrb([], NOW)


# --------------------------------------------------------------------------- #
# Belarusbank
# --------------------------------------------------------------------------- #
def test_belarusbank_parses_real_shape_and_scales_rub() -> None:
    rates = parse_belarusbank(BELARUSBANK_SAMPLE, NOW)
    assert isinstance(rates, BankRates) and rates.source == "Belarusbank"
    assert (rates.usd.buy, rates.usd.sell) == (pytest.approx(3.03), pytest.approx(3.07))
    assert (rates.eur.buy, rates.eur.sell) == (pytest.approx(3.38), pytest.approx(3.445))
    assert (rates.rub.buy, rates.rub.sell) == (pytest.approx(0.0315), pytest.approx(0.0359))  # за 1 RUB


def test_belarusbank_takes_median_over_branches_and_skips_junk() -> None:
    data = [
        belarusbank_branch(USD_in="3.0300"),
        belarusbank_branch(USD_in="3.0400"),
        belarusbank_branch(USD_in="3.1000"),  # выброс по одному отделению не сдвигает медиану
        {"filial_id": "broken"},
        "not-a-dict",
    ]
    assert parse_belarusbank(data, NOW).usd.buy == pytest.approx(3.04)


def test_belarusbank_never_reports_buy_above_sell() -> None:
    swapped = [belarusbank_branch(USD_in="3.0700", USD_out="3.0300")]
    usd = parse_belarusbank(swapped, NOW).usd
    assert usd.buy == pytest.approx(3.03) and usd.sell == pytest.approx(3.07)


def test_belarusbank_rub_already_per_one_is_not_scaled_twice() -> None:
    data = [belarusbank_branch(RUB_in="0.0315", RUB_out="0.0359")]
    rub = parse_belarusbank(data, NOW).rub
    assert rub.buy == pytest.approx(0.0315) and rub.sell == pytest.approx(0.0359)


def test_belarusbank_missing_optional_currency_is_none() -> None:
    row = belarusbank_branch()
    for key in ("EUR_in", "EUR_out"):
        del row[key]
    assert parse_belarusbank([row], NOW).eur is None


def test_belarusbank_rejects_empty_or_wrong_shape() -> None:
    for bad in ([], {}, "x", None):
        with pytest.raises(ParseError):
            parse_belarusbank(bad, NOW)
    with pytest.raises(ParseError, match="USD"):
        parse_belarusbank([{"filial_id": "1", "CAD_in": "2.1"}], NOW)


# --------------------------------------------------------------------------- #
# Международные резервные источники BYN
# --------------------------------------------------------------------------- #
def test_open_er_derives_byn_crosses() -> None:
    rates = parse_open_er(OPEN_ER_SAMPLE, NOW)
    assert rates.official is False
    assert rates.usd == pytest.approx(3.070244)
    assert rates.eur == pytest.approx(3.070244 / 0.8887)
    assert rates.rub == pytest.approx(3.070244 / 84.95)
    assert rates.rates_date == date(2026, 10, 7)


def test_open_er_errors() -> None:
    with pytest.raises(ParseError, match="unsupported"):
        parse_open_er({"result": "error", "error-type": "unsupported-code"}, NOW)
    with pytest.raises(ParseError, match="BYN"):
        parse_open_er({"result": "success", "rates": {"EUR": 0.9}}, NOW)


def test_currency_api_derives_byn_crosses() -> None:
    rates = parse_currency_api(CURRENCY_API_SAMPLE, NOW)
    assert rates.official is False
    assert rates.usd == pytest.approx(3.05072461)
    assert rates.eur == pytest.approx(3.05072461 / 0.8892)
    assert rates.rates_date == date(2026, 10, 6)
    with pytest.raises(ParseError, match="byn"):
        parse_currency_api({"date": "2026-10-06", "usd": {"eur": 0.9}}, NOW)


# --------------------------------------------------------------------------- #
# Провайдеры целиком (через FakeFetcher) — что отдаёт fetch() каждого
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider", DEFAULT_FIAT_PROVIDERS, ids=lambda p: p.key)
async def test_every_fiat_provider_fetches_expected_type(provider) -> None:
    result = await provider.fetch(FakeFetcher(), NOW)
    expected = BankRates if provider.kind == "bank" else FiatRates
    assert isinstance(result, expected)


def test_fiat_provider_kinds() -> None:
    assert {p.key: p.kind for p in DEFAULT_FIAT_PROVIDERS} == {
        "nbrb": "official", "belarusbank": "bank", "open_er": "fallback", "currency_api": "fallback",
    }
