"""Тексты сообщений бота (HTML). Всё, что видит пользователь, собрано здесь."""

from __future__ import annotations

from datetime import date, datetime, time, tzinfo
from typing import Iterable, Sequence

from .db import Alert, User
from .history import PriceStats
from .rates.models import Snapshot, SourceStatus
from .util import (
    display_name,
    esc,
    fmt_byn,
    fmt_int,
    fmt_pct,
    fmt_rate,
    fmt_usd,
    humanize_age,
    humanize_duration,
    span_label,
    sparkline,  # noqa: F401  (реэкспорт для удобства тестов)
    trend_icon,
)

FIAT_STALE_AFTER = 1800.0  # курсы BYN «устарели», если не обновлялись 30 минут
MAX_USERS_SHOWN = 40

DISCLAIMER = "<i>Данные носят справочный характер и не являются инвестиционной рекомендацией.</i>"

# (поле FiatRates, иконка, подпись, множитель «за N единиц»)
_FIAT_ROWS = (
    ("usd", "💵", "USD", 1),
    ("eur", "💶", "EUR", 1),
    ("rub", "🇷🇺", "100 RUB", 100),
)


# --------------------------------------------------------------------------- #
# Время
# --------------------------------------------------------------------------- #
def tz_label(tz_name: str) -> str:
    return "Минск" if tz_name == "Europe/Minsk" else tz_name


def fmt_clock(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def fmt_clock_s(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S")


def fmt_dt(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")


def fmt_bank(value: float) -> str:
    """3.020 → 3.02, 3.025 → 3.025 (банки котируют с 2–3 знаками)."""
    s = f"{value:.3f}"
    return s[:-1] if s.endswith("0") else s


# --------------------------------------------------------------------------- #
# Курсы
# --------------------------------------------------------------------------- #
def _btc_lines(snap: Snapshot, stats: PriceStats | None, now: float, stale_after: float) -> list[str]:
    btc = snap.btc
    out = ["₿ <b>Bitcoin</b>"]
    if btc is None:
        return out + ["нет данных — источники пока не ответили"]
    line = f"<b>{fmt_usd(btc.price)}</b>"
    if snap.btc_byn:
        line += f" · {fmt_byn(snap.btc_byn)}"
    out.append(line)
    if stats:
        out.append(f"{trend_icon(stats.change_pct)} {fmt_pct(stats.change_pct)} за {span_label(stats.span_hours)}")
        if stats.spark:
            out.append(f"<code>{stats.spark}</code>")
        out.append(f"↕️ {fmt_usd(stats.low)} – {fmt_usd(stats.high)}")
    age = now - btc.ts
    if age > stale_after:
        out.append(f"⚠️ данные устарели: обновлены {humanize_age(age)}")
    return out


def _fiat_lines(snap: Snapshot, now: float) -> list[str]:
    fiat = snap.fiat
    if fiat is None:
        return ["🏦 <b>Курсы BYN</b>", "нет данных — источники пока не ответили"]
    out: list[str] = []
    for attr, icon, label, per in _FIAT_ROWS:
        value = getattr(fiat, attr)
        if value is None:
            continue
        line = f"{icon} <b>{label}</b> — <b>{fmt_rate(value * per)}</b> BYN"
        bank = getattr(snap.bank, attr, None) if snap.bank else None
        if bank:
            line += f" · 🏦 {fmt_bank(bank.buy * per)} / {fmt_bank(bank.sell * per)}"
        out.append(line)
    src = f"{fiat.source} на {fiat.rates_date.strftime('%d.%m')}" if fiat.rates_date else fiat.source
    notes = [src]
    if snap.bank:
        notes.append("🏦 Belarusbank: покупка / продажа")
    out.append(f"<i>{esc(' · '.join(notes))}</i>")
    if not fiat.official:
        out.append("⚠️ НБРБ сейчас недоступен — показан международный курс")
    if now - fiat.ts > FIAT_STALE_AFTER:
        out.append(f"⚠️ курсы BYN обновлены {humanize_age(now - fiat.ts)}")
    return out


def render_rates(
    snap: Snapshot,
    stats: PriceStats | None,
    *,
    now: float,
    tz: tzinfo,
    title: str = "💱 Курсы сейчас",
    stale_after: float = 180.0,
) -> str:
    lines = [f"<b>{title}</b>", ""]
    lines += _btc_lines(snap, stats, now, stale_after)
    lines.append("")
    lines += _fiat_lines(snap, now)
    lines.append("")
    ref = snap.btc.ts if snap.btc else (snap.fiat.ts if snap.fiat else now)
    footer = f"Обновлено {fmt_clock_s(ref, tz)}"
    if snap.btc:
        footer += f" · BTC: {snap.btc.sources_used}/{snap.btc.sources_total} источников"
    lines.append(f"<i>{footer}</i>")
    return "\n".join(lines)


def render_menu(snap: Snapshot) -> str:
    lines = ["<b>₿ BTC Tracker</b>", ""]
    if snap.btc:
        line = f"₿ <b>{fmt_usd(snap.btc.price)}</b>"
        if snap.btc_byn:
            line += f" · {fmt_byn(snap.btc_byn)}"
        lines.append(line)
    fiat = snap.fiat
    if fiat and fiat.usd:
        parts = [f"💵 {fmt_rate(fiat.usd)}"]
        if fiat.eur:
            parts.append(f"💶 {fmt_rate(fiat.eur)}")
        if fiat.rub:
            parts.append(f"🇷🇺 {fmt_rate(fiat.rub * 100)}")
        lines.append(" · ".join(parts) + " BYN")
    if len(lines) == 2:
        lines.append("Курсы загружаются… попробуйте через минуту.")
    lines += ["", "Выберите действие 👇"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Приветствие и помощь
# --------------------------------------------------------------------------- #
def _times(slots: Iterable[time]) -> str:
    return ", ".join(t.strftime("%H:%M") for t in slots)


def render_welcome(slots: Sequence[time], tz_name: str) -> str:
    return (
        "👋 <b>BTC Tracker</b>\n\n"
        "Слежу за курсом <b>Bitcoin</b>, а также за долларом, евро и российским рублём к белорусскому рублю. "
        "Данные беру из нескольких источников и обновляю раз в минуту.\n\n"
        "🔔 <b>Алерт</b> — отправьте цену числом, например <code>90000</code>, "
        "и я напишу, когда BTC до неё дойдёт.\n"
        f"⏰ <b>Сводки</b> — каждый день в {_times(slots)} ({tz_label(tz_name)}).\n\n"
        "Управление — кнопками ниже 👇"
    )


def render_help(is_owner: bool, slots: Sequence[time], tz_name: str, max_alerts: int) -> str:
    text = (
        "ℹ️ <b>Как пользоваться</b>\n\n"
        "💱 <b>Курсы</b> — BTC (медиана по нескольким биржам), а также USD, EUR и 100 RUB к BYN: "
        "официальный курс НБРБ и курсы банка.\n\n"
        "🔔 <b>Алерты</b> — отправьте цену числом: <code>90000</code>, <code>90k</code> или <code>$90 000</code>. "
        "Направление (выше/ниже) определяю сам по текущей цене. В карточке алерта можно включить 🔁 повтор — "
        f"тогда я сообщаю при каждом пересечении уровня. До {max_alerts} алертов на человека.\n\n"
        f"⏰ <b>Сводки</b> — каждый день в {_times(slots)} ({tz_label(tz_name)}); время можно включать и выключать "
        "в разделе «Сводки».\n\n"
        "<b>Команды</b>\n"
        "/rates — курсы сейчас\n"
        "/alerts — мои алерты\n"
        "/alert 90000 — быстрый алерт\n"
        "/reports — время сводок\n"
        "/menu — главное меню\n"
        "/id — ваш Telegram ID"
    )
    if is_owner:
        text += (
            "\n\n<b>Для владельца</b>\n"
            "/invite — одноразовая ссылка-приглашение\n"
            "/users — список пользователей\n"
            "/adduser 123456789 — добавить по Telegram ID\n"
            "/removeuser 123456789 — убрать доступ\n"
            "/status — состояние источников данных"
        )
    return text + "\n\n" + DISCLAIMER


# --------------------------------------------------------------------------- #
# Алерты
# --------------------------------------------------------------------------- #
def alert_label(a: Alert, price: float | None = None) -> str:
    """Текст кнопки: «📈 ≥ $90 000 · +7.9% 🔁» (в кнопках HTML не работает)."""
    text = f"{'📈 ≥' if a.direction == 'above' else '📉 ≤'} {fmt_usd(a.target)}"
    if price:
        text += f" · {fmt_pct((a.target - price) / price * 100, 1)}"
    if a.repeat:
        text += " 🔁"
    return text


def render_alerts_list(alerts: Sequence[Alert], price: float | None, max_alerts: int) -> str:
    if not alerts:
        return (
            "🔔 <b>Алерты</b>\n\nПока нет ни одного алерта.\n\n"
            "Отправьте цену числом, например <code>90000</code> — и я напишу, когда BTC до неё дойдёт."
        )
    lines = [f"🔔 <b>Ваши алерты</b> ({len(alerts)}/{max_alerts})", ""]
    if price:
        lines.append(f"Сейчас BTC: <b>{fmt_usd(price)}</b>")
    lines.append("Нажмите на алерт, чтобы изменить или удалить.")
    return "\n".join(lines)


def render_new_alert_prompt(price: float | None) -> str:
    now_line = f"Сейчас BTC: <b>{fmt_usd(price)}</b>. " if price else ""
    return (
        "➕ <b>Новый алерт</b>\n\n"
        "Отправьте цену в долларах, при которой нужно уведомить, — например <code>90000</code> или <code>90k</code>.\n"
        f"{now_line}Выше текущей цены жду роста, ниже — падения.\n\n"
        "Или выберите изменение от текущей цены 👇"
    )


def render_alert_created(alert: Alert, price: float, *, duplicate: bool = False) -> str:
    rising = alert.direction == "above"
    head = "ℹ️ <b>Такой алерт уже есть</b>" if duplicate else "✅ <b>Алерт создан</b>"
    goal = "поднимется до" if rising else "опустится до"
    dist = fmt_pct((alert.target - price) / price * 100, 1)
    return (
        f"{head}\n\n"
        f"{'📈' if rising else '📉'} Сообщу, когда BTC {goal} <b>{fmt_usd(alert.target)}</b>\n"
        f"Сейчас {fmt_usd(price)} — до цели {dist}"
    )


def render_alert_detail(alert: Alert, price: float | None, tz: tzinfo) -> str:
    rising = alert.direction == "above"
    lines = [
        f"{'📈' if rising else '📉'} <b>Алерт: BTC {'≥' if rising else '≤'} {fmt_usd(alert.target)}</b>",
        "",
        "Жду роста до цели." if rising else "Жду падения до цели.",
    ]
    if price:
        lines.append(f"Сейчас: {fmt_usd(price)} (до цели {fmt_pct((alert.target - price) / price * 100, 1)})")
    lines.append("Режим: 🔁 повторяющийся" if alert.repeat else "Режим: разовый")
    lines.append(f"Создан: {fmt_dt(alert.created_at, tz)}")
    if alert.triggered_at:
        lines.append(
            f"Срабатывал: {fmt_dt(alert.triggered_at, tz)} при {fmt_usd(alert.triggered_price or alert.target)}"
            f" (раз: {alert.trigger_count})"
        )
    if not alert.active:
        lines += ["", "✅ Алерт сработал и выключен. Можно включить снова."]
    return "\n".join(lines)


def render_alert_fired(alert: Alert, price: float, stats: PriceStats | None, *, tz: tzinfo, now: float,
                       usd_byn: float | None = None) -> str:
    rising = alert.direction == "above"
    lines = [
        "🔔 <b>Алерт сработал!</b>",
        "",
        f"₿ Bitcoin {'вырос до' if rising else 'упал до'} <b>{fmt_usd(price)}</b>",
        f"Цель: {'≥' if rising else '≤'} {fmt_usd(alert.target)}",
    ]
    if usd_byn:
        lines.append(f"≈ {fmt_byn(price * usd_byn)}")
    if stats:
        lines.append(f"{trend_icon(stats.change_pct)} {fmt_pct(stats.change_pct)} за {span_label(stats.span_hours)}")
    if alert.repeat:
        lines.append("🔁 Повторяющийся алерт — сообщу при следующем пересечении уровня.")
    lines.append(f"<i>{fmt_clock_s(now, tz)}</i>")
    return "\n".join(lines)


ALERT_ERRORS = {
    "no_price": "⏳ Пока нет свежего курса BTC — попробуйте через минуту.",
    "equals_current": "🤔 Это и есть текущая цена. Укажите уровень выше или ниже.",
}


def render_alert_out_of_range(target: float, price: float, lo: float, hi: float) -> str:
    return (
        f"🤔 Цена {fmt_usd(target)} выглядит странно (сейчас BTC {fmt_usd(price)}).\n"
        f"Допустимо от {fmt_usd(lo)} до {fmt_usd(hi)}."
    )


def render_alert_limit(limit: int) -> str:
    return f"⚠️ Достигнут лимит — {limit} алертов. Удалите ненужные и попробуйте снова."


HINT_UNKNOWN = (
    "Не понял 🤔\n\nОтправьте цену числом — например <code>90000</code> — и я создам алерт, "
    "или пользуйтесь кнопками меню."
)


# --------------------------------------------------------------------------- #
# Сводки
# --------------------------------------------------------------------------- #
def render_reports_settings(slots: Sequence[time], off: frozenset[str], tz_name: str) -> str:
    enabled = [s.strftime("%H:%M") for s in slots if s.strftime("%H:%M") not in off]
    state = f"Сейчас включены: <b>{', '.join(enabled)}</b>" if enabled else "Сейчас все сводки <b>выключены</b>."
    return (
        "⏰ <b>Сводки о курсах</b>\n\n"
        f"Присылаю BTC, доллар, евро и рубль в это время ({tz_label(tz_name)}). "
        "Нажмите на время, чтобы включить или выключить.\n\n"
        f"{state}"
    )


# --------------------------------------------------------------------------- #
# Админка
# --------------------------------------------------------------------------- #
def render_admin(users_count: int, open_invites: int, alerts_count: int) -> str:
    return (
        "👑 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"🔔 Активных алертов: <b>{alerts_count}</b>\n"
        f"🔗 Действующих приглашений: <b>{open_invites}</b>\n\n"
        "<b>Как добавить человека</b>\n"
        "• «Пригласить» — получите одноразовую ссылку, перешлите её человеку\n"
        "• Человек сам нажмёт /start — вам придёт запрос с кнопкой «Разрешить»\n"
        "• «Добавить по ID» или команда <code>/adduser 123456789</code>"
    )


def render_users(users: Sequence[User], owner_ids: frozenset[int], tz: tzinfo) -> str:
    lines = [f"👥 <b>Пользователи</b> ({len(users)})", ""]
    for u in users[:MAX_USERS_SHOWN]:
        is_owner = u.user_id in owner_ids
        extra: list[str] = []
        if u.last_seen:
            extra.append(f"был {fmt_dt(u.last_seen, tz)}")
        elif not is_owner:
            extra.append("ещё не заходил")
        if u.bot_blocked:
            extra.append("заблокировал бота")
        name = display_name(u.full_name, u.username, u.user_id)
        lines.append(f"{'👑' if is_owner else '👤'} {name} · <code>{u.user_id}</code>" + (f" — {', '.join(extra)}" if extra else ""))
    if len(users) > MAX_USERS_SHOWN:
        lines.append(f"… и ещё {len(users) - MAX_USERS_SHOWN}")
    lines += ["", "Нажмите на имя, чтобы убрать доступ."]
    return "\n".join(lines)


def render_invite(link: str, ttl_hours: int) -> str:
    return (
        "➕ <b>Приглашение</b>\n\n"
        "Отправьте эту ссылку человеку — перейдя по ней, он сразу получит доступ к боту:\n\n"
        f"{esc(link)}\n\n"
        f"Ссылка одноразовая и действует {ttl_hours} ч."
    )


def render_joined_via_invite(user_id: int, full_name: str, username: str | None) -> str:
    return (
        "✅ <b>Новый пользователь по приглашению</b>\n\n"
        f"{display_name(full_name, username, user_id)}\nID: <code>{user_id}</code>"
    )


def render_access_request(user_id: int, full_name: str, username: str | None) -> str:
    return (
        "🔑 <b>Запрос доступа</b>\n\n"
        f"{display_name(full_name, username, user_id)}\nID: <code>{user_id}</code>\n\n"
        "Разрешить пользоваться ботом?"
    )


def render_add_user_prompt() -> str:
    return (
        "🆔 <b>Добавить по ID</b>\n\n"
        "Отправьте числовой Telegram ID человека. Узнать его можно, если человек напишет этому боту команду "
        "<code>/id</code> (или боту @userinfobot).\n\n"
        "Проще — воспользоваться приглашением по ссылке."
    )


def _source_line(name: str, st: SourceStatus | None, kind: str) -> str:
    if st is None:
        return f"⚪ {esc(name)} — не запрашивался"
    if st.ok:
        value = ""
        if st.value is not None:
            value = fmt_int(st.value) if kind == "btc" else fmt_rate(st.value)
        latency = f"{st.latency_ms} мс" if st.latency_ms is not None else ""
        return f"✅ {esc(name)} — " + " · ".join(x for x in (value, latency) if x)
    return f"❌ {esc(name)} — {esc(st.error or 'ошибка')} · сбоев подряд: {st.failures}"


def render_status(
    overview: Sequence[tuple[object, SourceStatus | None]],
    snap: Snapshot,
    *,
    now: float,
    started_at: float,
    users: int,
    alerts: int,
    version: str,
) -> str:
    btc = snap.btc
    head = "₿ <b>Bitcoin (USD)</b>"
    if btc:
        head += f" — источников {btc.sources_used}/{btc.sources_total}, медиана {fmt_usd(btc.price)}, разброс {btc.spread_pct:.2f}%"
    lines = ["📡 <b>Статус и источники</b>", "", head]
    for provider, st in overview:
        if getattr(provider, "group", "") == "btc":
            lines.append(_source_line(getattr(provider, "name", "?"), st, "btc"))
    lines += ["", "🏦 <b>Курсы BYN</b>"]
    for provider, st in overview:
        if getattr(provider, "group", "") == "fiat":
            lines.append(_source_line(getattr(provider, "name", "?"), st, "fiat"))
    lines += [
        "",
        f"🖥 v{esc(version)} · работает {humanize_duration(now - started_at)} · "
        f"пользователей: {users} · алертов: {alerts}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Старт бота и здоровье источников
# --------------------------------------------------------------------------- #
def render_startup_owner(
    snap: Snapshot,
    stats: PriceStats | None,
    *,
    now: float,
    tz: tzinfo,
    tz_name: str,
    prev_alive: float | None,
    version: str,
    stale_after: float = 180.0,
    booted_at: float | None = None,
) -> str:
    """`now` — момент доставки, `booted_at` — момент запуска бота. Они различаются, если сводка ждала
    интернет/Telegram: тогда в шапке показываем настоящее время запуска и поясняем задержку."""
    boot = now if booted_at is None else booted_at
    head = [f"🟢 <b>Бот запущен</b> · {fmt_clock(boot, tz)} ({tz_label(tz_name)}) · v{esc(version)}"]
    if prev_alive is None:
        head.append("Первый запуск.")
    else:
        gap = boot - prev_alive
        if gap >= 120:
            head.append(f"⏱ Простой: {humanize_duration(gap)} (с {fmt_dt(prev_alive, tz)}) — сервер был выключен или бот падал.")
        else:
            head.append("Перезапуск без простоя.")
    delay = now - boot
    if delay >= 120:
        head.append(f"📡 Сводка задержалась на {humanize_duration(delay)}: после запуска не было связи с источниками курсов или Telegram.")
    rates_block = render_rates(snap, stats, now=now, tz=tz, title="📊 Курсы сейчас", stale_after=stale_after)
    return "\n".join(head) + "\n\n" + rates_block


def render_startup_user(snap: Snapshot, stats: PriceStats | None, *, now: float, tz: tzinfo, stale_after: float = 180.0) -> str:
    rates_block = render_rates(snap, stats, now=now, tz=tz, title="📊 Курсы сейчас", stale_after=stale_after)
    return "🟢 <b>Бот снова в сети</b>\n\n" + rates_block


def render_startup_no_data(now: float, tz: tzinfo) -> str:
    return (
        f"🟢 <b>Бот запущен</b> · {fmt_clock(now, tz)}\n\n"
        "⚠️ Источники курсов пока не отвечают (нет интернета или блокировка?). "
        "Как только данные появятся — сразу пришлю сводку.\n"
        "Проверить доступность API с сервера: <code>python -m bot --check-apis</code>"
    )


def render_report(snap: Snapshot, stats: PriceStats | None, *, slot_label: str, now: float, tz: tzinfo,
                  late_minutes: int = 0, stale_after: float = 180.0) -> str:
    title = f"📊 Сводка · {slot_label}"
    text = render_rates(snap, stats, now=now, tz=tz, title=title, stale_after=stale_after)
    if late_minutes >= 3:
        text += f"\n<i>⏱ Сводка за {slot_label} отправлена с опозданием: был сбой связи или бот был недоступен.</i>"
    return text


def render_health_down(kind: str, minutes: int) -> str:
    if kind == "btc":
        return (
            f"⚠️ <b>Нет данных по BTC уже {minutes} мин</b>\n\n"
            "Ни один источник не отвечает. Алерты и сводки приостановлены до восстановления. "
            "Проверьте интернет на сервере: <code>python -m bot --check-apis</code>"
        )
    return f"⚠️ <b>Курсы BYN недоступны уже {minutes} мин</b> — НБРБ и резервные источники не отвечают."


def render_health_up(kind: str, minutes: int) -> str:
    what = "по BTC" if kind == "btc" else "по курсам BYN"
    return f"✅ Данные {what} восстановлены (перерыв ~{minutes} мин)."


# --------------------------------------------------------------------------- #
# Онбординг неавторизованных
# --------------------------------------------------------------------------- #
def render_id(user_id: int) -> str:
    return (
        f"🆔 Ваш Telegram ID: <code>{user_id}</code>\n\n"
        "Этот номер нужен владельцу бота, чтобы выдать вам доступ."
    )


def render_setup_mode(user_id: int) -> str:
    return (
        "🛠 <b>Бот ещё не настроен</b>\n\n"
        f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
        "Впишите его в файл <code>.env</code> как <code>OWNER_ID</code> и перезапустите бота — "
        "после этого доступ будет только у вас (а остальных вы сможете добавлять кнопками)."
    )


NO_ACCESS_TOAST = "⛔ Нет доступа"
ACCESS_REQUEST_SENT = (
    "🔒 <b>Это приватный бот.</b>\n\n"
    "Я отправил владельцу запрос на доступ — как только он ответит, я напишу вам здесь.\n"
    "Или попросите у владельца ссылку-приглашение."
)
ACCESS_PENDING = "⏳ Запрос уже отправлен владельцу — дождитесь ответа."
ACCESS_DENIED = "⛔ Доступ не предоставлен. Если это ошибка — обратитесь к владельцу бота."
ACCESS_GRANTED = "✅ <b>Доступ открыт!</b> Добро пожаловать."
ACCESS_REJECTED_NOTICE = "❌ Владелец отклонил запрос на доступ."
INVITE_INVALID = "❌ Ссылка-приглашение недействительна. Попросите владельца создать новую."
INVITE_USED = "❌ Эта ссылка уже использована. Попросите владельца прислать новую."
INVITE_EXPIRED = "⌛ Срок действия ссылки истёк. Попросите владельца прислать новую."
