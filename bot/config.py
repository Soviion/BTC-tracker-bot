"""Загрузка и проверка настроек из переменных окружения / файла .env."""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import time, timedelta, timezone, tzinfo
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .db import BIGINT_MAX

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 123456789:AAE... — формат токена от @BotFather
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
_PLACEHOLDER_PREFIXES = ("PASTE", "YOUR", "CHANGE", "<", "ВСТАВЬ", "ЗАМЕНИ")
_DSN_RE = re.compile(r"^postgres(?:ql)?://")

DEFAULT_REPORT_TIMES = "08:00,14:00,20:00,23:59"


class ConfigError(Exception):
    """Понятная человеку ошибка конфигурации."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_ids: frozenset[int]
    tz: tzinfo
    tz_name: str
    database_url: str = field(repr=False)
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_ssl: str | None = None  # None (из DSN) | "require" | "disable"
    db_disable_statement_cache: bool = False  # включить, если БД стоит за PgBouncer в transaction-режиме
    poll_interval: int = 60
    report_times: tuple[time, ...] = (time(8, 0), time(14, 0), time(20, 0), time(23, 59))
    report_grace_minutes: int = 15
    invite_ttl_hours: int = 24
    max_alerts_per_user: int = 25
    http_timeout: float = 6.0
    telegram_proxy: str | None = None
    api_proxy: str | None = None
    log_level: str = "INFO"
    log_file: Path | None = None
    # Кому слать сводку сразу после запуска (включение сервера, перезапуск): all | owner | off
    startup_report: str = "all"
    startup_min_gap: int = 180  # не чаще раза в N секунд — защита от спама при цикле перезапусков

    @property
    def setup_mode(self) -> bool:
        """Без OWNER_ID бот никому не даёт доступ — только подсказывает Telegram ID."""
        return not self.owner_ids

    @property
    def stale_after(self) -> int:
        """Через сколько секунд без обновления курс считается устаревшим."""
        return max(3 * self.poll_interval, 180)


# --------------------------------------------------------------------------- #
# Разбор отдельных значений
# --------------------------------------------------------------------------- #
def _is_placeholder(value: str) -> bool:
    v = value.strip()
    return not v or v.upper().startswith(_PLACEHOLDER_PREFIXES)


def parse_owner_ids(raw: str) -> frozenset[int]:
    """`123456789` или `123456789, 987654321`. Заглушка/пусто → пустое множество."""
    if _is_placeholder(raw):
        return frozenset()
    ids: set[int] = set()
    for part in re.split(r"[,\s;]+", raw.strip()):
        if not part:
            continue
        value = int(part) if part.isdigit() else None
        if value is None or not 0 < value <= BIGINT_MAX:
            raise ConfigError(
                f"OWNER_ID: «{part}» не похоже на Telegram ID. "
                "Нужно число, например 123456789 (узнать: отправьте /id вашему боту или @userinfobot)."
            )
        ids.add(value)
    return frozenset(ids)


def parse_report_times(raw: str) -> tuple[time, ...]:
    result: set[time] = set()
    for part in re.split(r"[,\s;]+", raw.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ConfigError(f"REPORT_TIMES: «{part}» — ожидается время в формате ЧЧ:ММ (например 08:00).")
        result.add(time(int(m.group(1)), int(m.group(2))))
    if not result:
        raise ConfigError("REPORT_TIMES: не указано ни одного времени.")
    return tuple(sorted(result))


def _load_tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name == "Europe/Minsk":  # в Беларуси нет перехода на летнее время: всегда UTC+3
            return timezone(timedelta(hours=3), "Europe/Minsk")
        raise ConfigError(
            f"TZ_NAME: часовой пояс «{name}» не найден. Пример: Europe/Minsk. "
            "На Windows/минимальных образах поможет `pip install tzdata`."
        ) from None


def _get_int(env: Mapping[str, str], key: str, default: int, *, lo: int, hi: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key}: «{raw}» — нужно целое число.") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{key}: значение должно быть от {lo} до {hi}.")
    return value


def _resolve_path(raw: str, default: Path) -> Path:
    if not raw.strip():
        return default
    p = Path(raw.strip()).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


# --------------------------------------------------------------------------- #
# Публичный API
# --------------------------------------------------------------------------- #
def load_settings(env: Mapping[str, str] | None = None, *, require_token: bool = True) -> Settings:
    """Читает настройки. `env=None` → .env (если есть) + переменные окружения."""
    if env is None:
        # Реальные переменные окружения (docker, systemd) важнее файла .env.
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        if Path.cwd().resolve() != PROJECT_ROOT:
            load_dotenv(Path.cwd() / ".env", override=False)
        env = os.environ

    token = env.get("BOT_TOKEN", "").strip()
    if require_token:
        if _is_placeholder(token):
            raise ConfigError(
                "Не задан BOT_TOKEN. Создайте бота у @BotFather (команда /newbot), "
                "скопируйте токен и вставьте в файл .env: BOT_TOKEN=123456:AA..."
            )
        if not TOKEN_RE.match(token):
            raise ConfigError(
                "BOT_TOKEN выглядит неправильно. Ожидается вид 123456789:AAEhBOweik6ad... "
                "(без кавычек и пробелов) — скопируйте токен из @BotFather целиком."
            )

    tz_name = env.get("TZ_NAME", "").strip() or "Europe/Minsk"

    database_url = env.get("DATABASE_URL", "").strip()
    if _is_placeholder(database_url):
        raise ConfigError(
            "Не задан DATABASE_URL. Укажите адрес вашей PostgreSQL, например:\n"
            "DATABASE_URL=postgresql://btc_bot:пароль@localhost:5432/btc_tracker\n"
            "(создайте пользователя и базу заранее: createuser/createdb или CREATE ROLE / CREATE DATABASE)."
        )
    if not _DSN_RE.match(database_url):
        raise ConfigError(
            "DATABASE_URL должен начинаться с postgresql:// (или postgres://), например "
            "postgresql://user:password@localhost:5432/dbname."
        )
    db_ssl = (env.get("DB_SSL", "").strip() or "").lower() or None
    if db_ssl not in (None, "require", "disable"):
        raise ConfigError("DB_SSL: допустимо require, disable или пусто (взять из DATABASE_URL).")

    log_level = (env.get("LOG_LEVEL", "").strip() or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError("LOG_LEVEL: допустимо DEBUG, INFO, WARNING или ERROR.")
    log_file_raw = env.get("LOG_FILE", "").strip()
    startup_report = (env.get("STARTUP_REPORT", "").strip() or "all").lower()
    if startup_report not in {"all", "owner", "off"}:
        raise ConfigError("STARTUP_REPORT: допустимо all (всем подписчикам), owner (только владельцу) или off.")

    db_pool_min = _get_int(env, "DB_POOL_MIN", 1, lo=1, hi=50)
    db_pool_max = _get_int(env, "DB_POOL_MAX", 10, lo=1, hi=100)
    if db_pool_max < db_pool_min:
        raise ConfigError("DB_POOL_MAX не может быть меньше DB_POOL_MIN.")

    return Settings(
        bot_token="" if _is_placeholder(token) else token,
        owner_ids=parse_owner_ids(env.get("OWNER_ID", "")),
        tz=_load_tz(tz_name),
        tz_name=tz_name,
        database_url=database_url,
        db_pool_min=db_pool_min,
        db_pool_max=db_pool_max,
        db_ssl=db_ssl,
        db_disable_statement_cache=env.get("DB_DISABLE_STATEMENT_CACHE", "").strip().lower() in ("1", "true", "yes"),
        poll_interval=_get_int(env, "POLL_INTERVAL", 60, lo=10, hi=3600),
        report_times=parse_report_times(env.get("REPORT_TIMES", "") or DEFAULT_REPORT_TIMES),
        report_grace_minutes=_get_int(env, "REPORT_GRACE_MINUTES", 15, lo=1, hi=120),
        invite_ttl_hours=_get_int(env, "INVITE_TTL_HOURS", 24, lo=1, hi=24 * 30),
        max_alerts_per_user=_get_int(env, "MAX_ALERTS_PER_USER", 25, lo=1, hi=500),
        http_timeout=float(_get_int(env, "HTTP_TIMEOUT", 6, lo=2, hi=30)),
        telegram_proxy=env.get("TELEGRAM_PROXY", "").strip() or None,
        api_proxy=env.get("API_PROXY", "").strip() or None,
        log_level=log_level,
        log_file=_resolve_path(log_file_raw, Path()) if log_file_raw else None,
        startup_report=startup_report,
        startup_min_gap=_get_int(env, "STARTUP_MIN_GAP", 180, lo=0, hi=86400),
    )


def configure_logging(settings: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(settings.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=settings.log_level, handlers=handlers, force=True)
    # aiogram пишет INFO на каждый апдейт — для долгоживущего бота это шум (DEBUG оставляем как есть).
    if settings.log_level != "DEBUG":
        logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
