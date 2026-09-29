from __future__ import annotations

import pytest

from bot.rates.aggregate import aggregate_prices


def test_empty_returns_none() -> None:
    assert aggregate_prices({}) is None


def test_single_source_used_as_is() -> None:
    agg = aggregate_prices({"binance": 83000.0})
    assert agg.price == 83000.0
    assert agg.used == {"binance": 83000.0}
    assert agg.rejected == {}
    assert agg.spread_pct == 0.0


def test_two_close_sources_both_used_and_averaged() -> None:
    agg = aggregate_prices({"a": 83000.0, "b": 83100.0})
    assert agg.used == {"a": 83000.0, "b": 83100.0}
    assert agg.price == pytest.approx(83050.0)


def test_two_far_sources_with_reference_pick_closest() -> None:
    agg = aggregate_prices({"a": 83000.0, "bad": 50000.0}, reference=82900.0, max_dev_pct=3.0)
    assert agg.used == {"a": 83000.0}
    assert agg.rejected == {"bad": 50000.0}
    assert agg.price == 83000.0


def test_two_far_sources_without_reference_keep_both() -> None:
    # без ориентира (первый опрос после запуска бота) нечем решить, кто прав — доверяем обоим
    agg = aggregate_prices({"a": 83000.0, "b": 50000.0}, reference=None)
    assert agg.used == {"a": 83000.0, "b": 50000.0}
    assert agg.rejected == {}


def test_three_sources_outlier_rejected() -> None:
    agg = aggregate_prices({"a": 83000.0, "b": 83050.0, "c": 120000.0}, max_dev_pct=3.0)
    assert "c" in agg.rejected
    assert set(agg.used) == {"a", "b"}
    assert agg.price == pytest.approx(83025.0)


def test_three_sources_all_within_tolerance() -> None:
    agg = aggregate_prices({"a": 83000.0, "b": 83100.0, "c": 82950.0})
    assert set(agg.used) == {"a", "b", "c"}
    assert agg.rejected == {}


def test_all_identical_zero_spread() -> None:
    agg = aggregate_prices({"a": 83000.0, "b": 83000.0, "c": 83000.0})
    assert agg.price == 83000.0
    assert agg.spread_pct == 0.0


def test_empty_after_filter_falls_back_to_full_set() -> None:
    # чётное число источников двумя далёкими кластерами: интерполированная медиана лежит в «пустоте»
    # между ними, и относительно неё ВСЕ источники — выбросы. Страховка: не возвращать пустой результат.
    agg = aggregate_prices({"a": 10000.0, "b": 10001.0, "c": 90000.0, "d": 90001.0}, max_dev_pct=3.0)
    assert set(agg.used) == {"a", "b", "c", "d"}
    assert agg.rejected == {}
    assert agg.price == pytest.approx(50000.5)
