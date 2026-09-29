"""Сборка всех роутеров aiogram в один. Порядок важен: fallback ловит всё нераспознанное и должен идти последним."""

from __future__ import annotations

from aiogram import Router

from . import admin, alerts, common, fallback, rates, reports


def build_router() -> Router:
    root = Router(name="root")
    for module in (common, rates, alerts, reports, admin, fallback):
        root.include_router(module.router)
    return root
