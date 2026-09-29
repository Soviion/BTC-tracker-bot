"""`python -m bot` — запуск бота. `python -m bot --check-apis` — проверка источников без Telegram и БД."""

from __future__ import annotations

import asyncio
import sys


def _run_check_apis() -> int:
    from .diagnostics import check_apis

    ok = asyncio.run(check_apis())
    return 0 if ok else 1


def main() -> None:
    if "--check-apis" in sys.argv[1:]:
        sys.exit(_run_check_apis())
    from .main import main as run_bot

    run_bot()


if __name__ == "__main__":
    main()
