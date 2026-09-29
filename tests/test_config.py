from __future__ import annotations

from datetime import time

import pytest

from bot.config import ConfigError, load_settings, parse_owner_ids, parse_report_times

VALID_TOKEN = "123456789:AAEhBOweik6ad-exampleexampleexample"
VALID_DSN = "postgresql://btc_bot:secret@localhost:5432/btc_tracker"


def base_env(**overrides: str) -> dict[str, str]:
    env = {"BOT_TOKEN": VALID_TOKEN, "OWNER_ID": "123456789", "DATABASE_URL": VALID_DSN}
    env.update(overrides)
    return env


def test_parse_owner_ids_variants() -> None:
    assert parse_owner_ids("123456789") == {123456789}
    assert parse_owner_ids("123, 456;789") == {123, 456, 789}
    assert parse_owner_ids("") == frozenset()
    assert parse_owner_ids("PASTE_YOUR_ID_HERE") == frozenset()
    with pytest.raises(ConfigError):
        parse_owner_ids("not-a-number")
    with pytest.raises(ConfigError):
        parse_owner_ids("-5")


def test_parse_report_times_variants() -> None:
    assert parse_report_times("08:00,14:00,20:00,23:59") == (time(8, 0), time(14, 0), time(20, 0), time(23, 59))
    assert parse_report_times("09:00") == (time(9, 0),)
    with pytest.raises(ConfigError):
        parse_report_times("25:00")
    with pytest.raises(ConfigError):
        parse_report_times("08:60")
    with pytest.raises(ConfigError):
        parse_report_times("not-a-time")
    with pytest.raises(ConfigError):
        parse_report_times("")


def test_load_settings_minimal_valid_env() -> None:
    settings = load_settings(base_env())
    assert settings.bot_token == VALID_TOKEN
    assert settings.owner_ids == {123456789}
    assert settings.database_url == VALID_DSN
    assert settings.tz_name == "Europe/Minsk"
    assert settings.poll_interval == 60
    assert settings.report_times == (time(8, 0), time(14, 0), time(20, 0), time(23, 59))
    assert settings.setup_mode is False


def test_missing_or_placeholder_token_raises() -> None:
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        load_settings(base_env(BOT_TOKEN=""))
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        load_settings(base_env(BOT_TOKEN="PASTE_YOUR_TOKEN_HERE"))


def test_malformed_token_raises() -> None:
    with pytest.raises(ConfigError):
        load_settings(base_env(BOT_TOKEN="not-a-real-token"))


def test_missing_database_url_raises() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings(base_env(DATABASE_URL=""))


def test_non_postgres_database_url_raises() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings(base_env(DATABASE_URL="mysql://user:pw@localhost/db"))


def test_no_owner_id_is_setup_mode_not_error() -> None:
    settings = load_settings(base_env(OWNER_ID=""))
    assert settings.setup_mode is True
    assert settings.owner_ids == frozenset()


def test_db_pool_max_below_min_raises() -> None:
    with pytest.raises(ConfigError, match="DB_POOL_MAX"):
        load_settings(base_env(DB_POOL_MIN="10", DB_POOL_MAX="2"))


def test_invalid_log_level_raises() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_settings(base_env(LOG_LEVEL="VERBOSE"))


def test_invalid_startup_report_raises() -> None:
    with pytest.raises(ConfigError, match="STARTUP_REPORT"):
        load_settings(base_env(STARTUP_REPORT="everyone"))


def test_invalid_db_ssl_raises() -> None:
    with pytest.raises(ConfigError, match="DB_SSL"):
        load_settings(base_env(DB_SSL="maybe"))


def test_report_grace_minutes_is_configurable() -> None:
    assert load_settings(base_env()).report_grace_minutes == 15
    assert load_settings(base_env(REPORT_GRACE_MINUTES="30")).report_grace_minutes == 30
    with pytest.raises(ConfigError, match="REPORT_GRACE_MINUTES"):
        load_settings(base_env(REPORT_GRACE_MINUTES="0"))


def test_stale_after_scales_with_poll_interval() -> None:
    settings = load_settings(base_env(POLL_INTERVAL="120"))
    assert settings.stale_after == 360
    settings_min = load_settings(base_env(POLL_INTERVAL="10"))
    assert settings_min.stale_after == 180  # нижняя граница
