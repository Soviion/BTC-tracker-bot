"""Мелкие чистые функции: разбор цены, форматирование чисел и времени."""

from __future__ import annotations

import html
import math
import re
from typing import Sequence

NBSP = " "
_SPARK_BARS = "▁▂▃▄▅▆▇█"

# «$ 90 000», «90k», «1,5к», «90000 usd», «90 тыс» не поддерживаем намеренно — только понятные формы.
_PRICE_RE = re.compile(
    r"""^\s*\$?\s*
        (?P<num>\d[\d\s  .,]*?)
        \s*(?P<mult>[kкmм])?
        \s*(?:\$|usd|usdt|dollars?|дол\w*|бакс\w*)?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_MULTIPLIERS = {"k": 1_000, "к": 1_000, "m": 1_000_000, "м": 1_000_000}


def _number_to_float(num: str) -> float | None:
    """Разбирает «90 000», «90,000», «90.000», «1 234,56», «1,234.56» и т.п."""
    s = re.sub(r"[\s  ]", "", num)
    if not s:
        return None
    dots, commas = s.count("."), s.count(",")
    if dots and commas:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thou = "," if dec == "." else "."
        head, _, tail = s.rpartition(dec)
        if thou in tail or dec in head:
            return None
        s = head.replace(thou, "") + "." + tail
    elif dots or commas:
        sep = "." if dots else ","
        if (dots or commas) > 1:  # несколько одинаковых разделителей — это разряды
            parts = s.split(sep)
            if not 1 <= len(parts[0]) <= 3 or any(len(p) != 3 for p in parts[1:]):
                return None
            s = "".join(parts)
        else:
            head, tail = s.split(sep)
            if len(tail) == 3 and head not in ("", "0"):  # 90.000 / 90,000 → девяносто тысяч
                s = head + tail
            else:
                s = (head or "0") + "." + tail
    try:
        value = float(s)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_price(text: str) -> float | None:
    """Достаёт цену в долларах из пользовательского ввода. None — если не похоже на цену."""
    m = _PRICE_RE.match(text or "")
    if not m:
        return None
    value = _number_to_float(m.group("num"))
    if value is None:
        return None
    mult = m.group("mult")
    if mult:
        value *= _MULTIPLIERS[mult.lower()]
    if not math.isfinite(value) or value <= 0:
        return None
    return round(value, 2)


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #
def fmt_int(value: float) -> str:
    return f"{value:,.0f}".replace(",", NBSP)


def fmt_usd(value: float) -> str:
    """$83 418 (для дробных цен — с копейками)."""
    if value >= 1000:
        return f"${fmt_int(value)}"
    return "$" + f"{value:,.2f}".replace(",", NBSP)


def fmt_byn(value: float) -> str:
    return f"{fmt_int(value)} BYN"


def fmt_rate(value: float, digits: int = 4) -> str:
    return f"{value:.{digits}f}"


def fmt_pct(value: float, digits: int = 2) -> str:
    """+1.24% / −0.50% (настоящий минус, чтобы не терялся в шрифте)."""
    text = f"{abs(value):.{digits}f}%"
    if round(value, digits) == 0:
        return text
    return ("+" if value > 0 else "−") + text


def trend_icon(value: float) -> str:
    if value > 0.05:
        return "🟢"
    if value < -0.05:
        return "🔴"
    return "⚪"


def sparkline(values: Sequence[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return _SPARK_BARS[3] * len(values)
    top = len(_SPARK_BARS) - 1
    return "".join(_SPARK_BARS[min(top, int((v - lo) / (hi - lo) * top + 0.5))] for v in values)


def span_label(hours: float) -> str:
    """Подпись периода: 24ч / 5ч / 40мин."""
    if hours >= 23:
        return "24ч"
    if hours >= 1:
        return f"{round(hours)}ч"
    return f"{max(1, round(hours * 60))}мин"


def humanize_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 45:
        return "только что"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} мин назад"
    if seconds < 86400:
        h, m = divmod(seconds // 60, 60)
        return f"{h} ч {m} мин назад" if m else f"{h} ч назад"
    return f"{seconds // 86400} дн назад"


def humanize_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} дн {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def display_name(full_name: str, username: str | None, user_id: int) -> str:
    """«Имя (@username)» или «id 123» — безопасно для HTML."""
    name = esc(full_name.strip()) or f"id {user_id}"
    return f"{name} (@{esc(username)})" if username else name
