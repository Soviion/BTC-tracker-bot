from __future__ import annotations

import pytest

from bot.history import compute_stats


def test_empty_or_single_point_returns_none() -> None:
    assert compute_stats([], now=1000, current=83000) is None
    assert compute_stats([(900, 83000)], now=1000, current=83000) is None


def test_too_short_span_returns_none() -> None:
    now = 100_000
    series = [(now - 60, 83000), (now - 30, 83010)]  # всего минута данных, min_span=1800
    assert compute_stats(series, now=now, current=83020) is None


def test_change_pct_and_range() -> None:
    now = 100_000
    series = [(now - 3600, 80000.0), (now - 1800, 82000.0)]
    stats = compute_stats(series, now=now, current=84000.0)
    assert stats is not None
    assert stats.change_pct == pytest.approx((84000.0 - 80000.0) / 80000.0 * 100)
    assert stats.low == 80000.0
    assert stats.high == 84000.0
    assert stats.span_hours == pytest.approx(1.0, abs=0.01)


def test_spark_only_generated_for_long_enough_span() -> None:
    now = 100_000
    short_series = [(now - 3000, 83000.0), (now - 2000, 83100.0)]  # >30мин, но <2ч
    short_stats = compute_stats(short_series, now=now, current=83200.0)
    assert short_stats is not None
    assert short_stats.spark == ""

    long_series = [(now - i * 600, 83000.0 + i * 10) for i in range(20, 0, -1)]  # ~3.3 часа истории
    long_stats = compute_stats(long_series, now=now, current=83500.0)
    assert long_stats is not None
    assert long_stats.spark != ""
    assert len(long_stats.spark) == 24  # buckets по умолчанию


def test_window_excludes_old_points() -> None:
    now = 200_000
    series = [(now - 200_000, 1.0), (now - 3600, 83000.0), (now - 1800, 83100.0)]
    stats = compute_stats(series, now=now, current=83200.0, window=86400)
    assert stats is not None
    assert stats.low == 83000.0  # точка суточной давности не должна попасть в окно
