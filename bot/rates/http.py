"""Тонкая обёртка над aiohttp для запросов к JSON API + расшифровка ошибок."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Protocol, TypeVar

import aiohttp

USER_AGENT = "Mozilla/5.0 (compatible; btc-tracker-bot/1.0)"
_T = TypeVar("_T")


class FetchError(Exception):
    """Источник ответил, но не так, как надо (HTTP-статус, не JSON, ...)."""


class ParseError(ValueError):
    """Ответ получен, но структура не совпала с ожидаемой."""


class Fetcher(Protocol):
    async def get_json(self, url: str, *, params: Mapping[str, str] | None = None) -> Any: ...


class JsonFetcher:
    """Общая HTTP-сессия для всех источников. Создаётся лениво внутри работающего event loop."""

    def __init__(self, *, timeout: float = 6.0, proxy: str | None = None) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._proxy = proxy
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.5"},
                connector=aiohttp.TCPConnector(limit=40, ttl_dns_cache=300),
            )
        return self._session

    async def get_json(self, url: str, *, params: Mapping[str, str] | None = None) -> Any:
        session = self._ensure_session()
        async with session.get(url, params=params, proxy=self._proxy) as resp:
            if resp.status != 200:
                raise FetchError(f"HTTP {resp.status}")
            try:
                return await resp.json(content_type=None)
            except (ValueError, aiohttp.ClientError) as exc:
                raise FetchError("ответ не в формате JSON") from exc

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


async def fetch_with_mirrors(
    fetcher: Fetcher,
    urls: tuple[str, ...],
    parse: Callable[[Any], _T],
    params: Mapping[str, str] | None = None,
) -> _T:
    """Пробует адреса по очереди (основной, затем зеркала); возвращает первый удачный разбор."""
    last_exc: Exception | None = None
    for url in urls:
        try:
            return parse(await fetcher.get_json(url, params=params))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — любой сбой источника не должен ронять опрос
            last_exc = exc
    assert last_exc is not None, "urls must not be empty"
    raise last_exc


def describe_error(exc: BaseException) -> str:
    """Короткое человекочитаемое описание ошибки источника (для логов и экрана статуса)."""
    if isinstance(exc, FetchError):
        return str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "таймаут"
    if isinstance(exc, aiohttp.ClientSSLError):
        return "ошибка SSL-сертификата"
    if isinstance(exc, aiohttp.ClientConnectorError):
        return "нет соединения"
    if isinstance(exc, aiohttp.ClientError):
        return f"сетевая ошибка ({type(exc).__name__})"
    if isinstance(exc, ParseError):
        return f"неожиданный ответ: {exc}"
    if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
        return f"неожиданный формат ({type(exc).__name__}: {exc})"[:120]
    return f"{type(exc).__name__}: {exc}"[:120]
