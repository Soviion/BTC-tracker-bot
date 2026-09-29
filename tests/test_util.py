from __future__ import annotations

import pytest

from bot.util import (
    display_name,
    esc,
    fmt_pct,
    fmt_usd,
    humanize_age,
    humanize_duration,
    parse_price,
    span_label,
    sparkline,
    trend_icon,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("90000", 90000.0),
        ("90 000", 90000.0),
        ("90 000", 90000.0),  # неразрывный пробел, как в сообщениях от бота
        ("90,000", 90000.0),
        ("90.000", 90000.0),  # европейский стиль: точка — разделитель разрядов
        ("90000.5", 90000.5),
        ("90000,50", 90000.50),
        ("90,000.50", 90000.50),
        ("1 234,56", 1234.56),
        ("1.234.567", 1234567.0),
        ("90k", 90000.0),
        ("90K", 90000.0),
        ("90к", 90000.0),  # кириллическая «к»
        ("1.5k", 1500.0),
        ("1,5k", 1500.0),
        ("$90000", 90000.0),
        ("90000$", 90000.0),
        (" 90000 ", 90000.0),
        ("90000 usd", 90000.0),
        ("90000usdt", 90000.0),
        ("90000 долларов", 90000.0),
        ("90000 баксов", 90000.0),
        ("90k$", 90000.0),
        # синтаксически валидное, хоть и абсурдное число — parse_price не проверяет разумность цены,
        # это осознанно оставлено AlertService (MIN/MAX_TARGET_RATIO от текущего курса)
        ("90 000 000 000 000", 90_000_000_000_000.0),
    ],
)
def test_parse_price_accepts(text: str, expected: float) -> None:
    assert parse_price(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "-5", "-90000", "0", "0.0", "90000usd extra junk", "k90000", "$"],
)
def test_parse_price_rejects(text: str) -> None:
    assert parse_price(text) is None


def test_fmt_usd_switches_to_cents_below_1000() -> None:
    assert fmt_usd(83418.0) == "$83 418"
    assert fmt_usd(5.5) == "$5.50"


def test_fmt_pct_sign_and_zero() -> None:
    assert fmt_pct(1.2345, 2) == "+1.23%"
    assert fmt_pct(-1.2345, 2) == "−1.23%"
    assert fmt_pct(0.0, 2) == "0.00%"
    assert fmt_pct(0.001, 2) == "0.00%"  # округляется в ноль — знака быть не должно


def test_trend_icon_thresholds() -> None:
    assert trend_icon(1.0) == "🟢"
    assert trend_icon(-1.0) == "🔴"
    assert trend_icon(0.0) == "⚪"


def test_sparkline_length_and_flat() -> None:
    assert sparkline([]) == ""
    assert len(sparkline([1, 2, 3, 4, 5])) == 5
    assert len(set(sparkline([5, 5, 5]))) == 1  # плоская линия — один и тот же символ


def test_span_label() -> None:
    assert span_label(24) == "24ч"
    assert span_label(5) == "5ч"
    assert span_label(0.5) == "30мин"


def test_humanize_age_buckets() -> None:
    assert humanize_age(10) == "только что"
    assert "мин назад" in humanize_age(120)
    assert "ч" in humanize_age(3 * 3600 + 60)
    assert "дн назад" in humanize_age(2 * 86400)


def test_humanize_duration_buckets() -> None:
    assert humanize_duration(90) == "1 мин"
    assert humanize_duration(3661) == "1 ч 1 мин"
    assert humanize_duration(2 * 86400 + 3600) == "2 дн 1 ч"


def test_esc_escapes_html_but_not_quotes() -> None:
    assert esc("<script>&") == "&lt;script&gt;&amp;"


def test_display_name_with_and_without_username() -> None:
    assert display_name("Alex <3", "alex_dev", 1) == "Alex &lt;3 (@alex_dev)"
    assert display_name("", None, 42) == "id 42"
