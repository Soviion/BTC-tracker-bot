"""Проверка BOT_TOKEN при старте: понятный выход при неверном токене и терпеливое ожидание сети."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from bot.main import _verify_token_or_exit

ME = SimpleNamespace(username="btc_bot", id=42)


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    """Подменяет aiogram.Bot: me() выдаёт заготовленные результаты по очереди (исключение — бросается)."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.session = FakeSession()

    async def me(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(GetMe(), "Cannot connect to host api.telegram.org")


async def test_ok_on_first_attempt() -> None:
    bot = FakeBot([ME])
    await _verify_token_or_exit(bot)
    assert bot.calls == 1


async def test_waits_for_network_instead_of_crashing() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    bot = FakeBot([network_error(), network_error(), network_error(), ME])
    await _verify_token_or_exit(bot, retry_delay=7.0, sleep=sleep)

    assert bot.calls == 4
    assert sleeps == [7.0, 7.0, 7.0]


async def test_wrong_token_exits_with_code_1_and_clear_message(capsys: pytest.CaptureFixture[str]) -> None:
    bot = FakeBot([TelegramUnauthorizedError(GetMe(), "Unauthorized")])

    with pytest.raises(SystemExit) as exc_info:
        await _verify_token_or_exit(bot)

    assert exc_info.value.code == 1
    assert bot.session.closed is True
    assert "BOT_TOKEN" in capsys.readouterr().err
    assert bot.calls == 1  # неверный токен повторять бесполезно


async def test_wrong_token_after_network_errors_still_exits() -> None:
    async def sleep(seconds: float) -> None:
        return None

    bot = FakeBot([network_error(), TelegramUnauthorizedError(GetMe(), "Unauthorized")])
    with pytest.raises(SystemExit) as exc_info:
        await _verify_token_or_exit(bot, sleep=sleep)
    assert exc_info.value.code == 1
