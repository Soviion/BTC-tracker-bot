"""Типизированные данные для inline-кнопок (влезают в лимит Telegram в 64 байта)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="nav"):
    to: str  # menu | rates | alerts | reports | admin | help


class AlertCb(CallbackData, prefix="al"):
    act: str  # new | open | rep | del | delyes | pct | clear | clearyes
    id: int = 0
    val: int = 0  # для pct: -10, -5, -1, 1, 5, 10


class ReportCb(CallbackData, prefix="rp"):
    act: str  # t (переключить слот) | on | off
    slot: str = ""  # "0800" — без двоеточия, оно разделитель в callback_data


class AdminCb(CallbackData, prefix="ad"):
    act: str  # users | invite | revoke | add | rm | rmyes | status
    uid: int = 0


class RequestCb(CallbackData, prefix="rq"):
    act: str  # ok | no
    uid: int
