"""Бот «Не выставлены счета»: сводка в канал Telegram по книге-сводке, каждый будний день.

Что и когда
───────────
В будни после `FINANCE_BILLS_HOUR` (по Астане) бот кладёт в канал один пост:
просроченные счета по юрлицам - договор строкой, месяцы через запятую, - и те,
что выставлять в ближайшие дни. Список - тот же, что в «Счета → Не
выставлены» (`contracts/bills.py`): одна книга, одни слова, одно чтение.

Дальше, до конца дня, бот **правит этот же пост**, когда книга меняется:
поставили галочку «Счет» - строка уходит из поста. Новых сообщений за день
нет: канал - напоминание, а не лента, и второй пост за день приучил бы его
не читать.

Один пост на день
─────────────────
«Сводка за сегодня ушла» помнит база (`counters`: `bills.day`, `bills.post`,
`bills.hash`), а не память процесса: перезапуск или два экземпляра API на
время выкладки не выложат её дважды. Telegram не принял - день освобождается,
и следующий проход (через минуту) пробует снова.

Пустой список не выкладывается: «всё выставлено» каждое утро - шум, который
перестают читать раньше, чем он понадобится.

В пост влезает не больше `LIMIT` знаков (Telegram режет на 4096). Не влезло
- хвост договоров заменяется строкой «и ещё N - в KORT»: полный список - по
кнопке «Открыть в KORT».
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.finance.config import finance_settings
from app.finance.contracts import bills
from app.finance.contracts.models import Counter, SummarySource
from app.finance.db import finance_session
from app.finance.support_bot import TZ, Bot, TelegramError

log = logging.getLogger(__name__)

#: Сколько знаков поста занимает список. Telegram принимает 4096; запас - на
#: шапку и подвал, которые дописываются после.
LIMIT = 3700
#: Позже этого часа сводка дня не выкладывается: пост в 23:00 никто не прочтёт
#: как утреннее напоминание.
LAST_HOUR = 20
#: Как часто проверять, не пора ли выложить или поправить пост.
TICK = 60
#: Как часто перечитывать сведения о канале (название, ссылка) для «Счетов».
CHANNEL_TTL = 3600.0

DAY = "bills.day"
POST = "bills.post"
HASH = "bills.hash"

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = {
    "январь": "янв",
    "февраль": "фев",
    "март": "мар",
    "апрель": "апр",
    "май": "май",
    "июнь": "июн",
    "июль": "июл",
    "август": "авг",
    "сентябрь": "сен",
    "октябрь": "окт",
    "ноябрь": "ноя",
    "декабрь": "дек",
}


def _clean(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def money(value: Decimal) -> str:
    """«2 345 000»; копейки - только если они есть."""
    whole = value.quantize(Decimal("1"))
    if whole == value:
        return f"{int(whole):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ")


def app_url() -> str:
    """Адрес KORT для кнопки: `FINANCE_APP_URL`, иначе первый https из ALLOWED_ORIGINS."""
    raw = _clean(finance_settings.app_url).rstrip("/")
    if raw:
        return raw
    from app.core.config import settings

    return next((origin.rstrip("/") for origin in settings.cors_origins if origin.startswith("https://")), "")


def section_link() -> str:
    base = app_url()
    return f"{base}/?s=invoices" if base.startswith("https://") else ""


# ── Пост ─────────────────────────────────────────────────────────────────────


def month_label(bill: bills.Bill) -> str:
    """«ОКТЯБРЬ 2026» → «окт»; «Старые (до Май/Июня 2026)» → «старые»."""
    words = bill.month.lower().split()
    if not words:
        return bill.period_from.strftime("%m.%Y") if bill.period_from else "?"
    return MONTHS.get(words[0], words[0])


def months_text(items: list[bills.Bill]) -> str:
    """Месяцы договора через запятую; три и больше подряд - «июн-окт»."""
    ordered = sorted(items, key=lambda bill: (bill.month_no or 0, bill.period_from or date.min))
    parts: list[str] = []
    run: list[bills.Bill] = []

    def flush() -> None:
        if len(run) >= 3:
            parts.append(f"{month_label(run[0])}-{month_label(run[-1])}")
        else:
            parts.extend(month_label(bill) for bill in run)
        run.clear()

    for bill in ordered:
        regular = month_label(bill) in MONTHS.values() and bill.month_no is not None
        if run and regular and bill.month_no == (run[-1].month_no or 0) + 1:
            run.append(bill)
            continue
        flush()
        if regular:
            run.append(bill)
        else:
            parts.append(month_label(bill))
    flush()
    return ", ".join(dict.fromkeys(parts))


def person(staff: str) -> str:
    """Сотрудник из книги - если это человек: у аренды там стоит юрлицо («ТОО "…"»)."""
    text = staff.strip()
    if not text or '"' in text or text.upper().startswith(("ТОО", "TOO", "ИП ")):
        return ""
    return text


@dataclass(frozen=True)
class Contract:
    own: str
    customer: str
    number: str
    items: tuple[bills.Bill, ...]

    @property
    def total(self) -> Decimal:
        return sum((bill.amount or Decimal("0") for bill in self.items), Decimal("0"))

    def line(self) -> str:
        parts = [f"<b>{_esc(self.customer)}</b>"]
        if self.number:
            parts.append(_esc(self.number))
        parts.append(_esc(months_text(list(self.items))))
        if self.total:
            parts.append(money(self.total))
        staff = next((person(bill.staff) for bill in self.items if person(bill.staff)), "")
        if staff:
            parts.append(_esc(staff))
        return "• " + " · ".join(parts)


def contracts(items: Iterable[bills.Bill]) -> list[Contract]:
    """Строки книги - договорами: один договор за пять месяцев - одна строка поста."""
    grouped: dict[tuple[str, str, str], list[bills.Bill]] = {}
    for bill in items:
        grouped.setdefault((bill.own, bill.customer, bill.number), []).append(bill)
    return [Contract(own, customer, number, tuple(rows)) for (own, customer, number), rows in grouped.items()]


@dataclass(frozen=True)
class Post:
    body: str
    text: str

    @property
    def digest(self) -> int:
        """Отпечаток списка - чтобы править пост, только когда список изменился."""
        return int(hashlib.sha1(self.body.encode()).hexdigest()[:15], 16)


def plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)


def _soon_line(bill: bills.Bill) -> str:
    """Срок - словами книги («3 дня до выставления» → «через 3 дня»), а не датой периода.

    Книги юрлиц считают срок по-своему (у одних от «Периода с», у других от
    «Числа выставления»), и дата периода в посте спорила бы с книгой на день.
    """
    text = bill.text.strip()
    marker = text.lower().find(" до выставления")
    when = f"через {text[:marker]}" if marker > 0 else text
    parts = (_esc(when), f"<b>{_esc(bill.customer)}</b>", _esc(bill.number), _esc(bill.own))
    return "• " + " · ".join(part for part in parts if part)


def render(board: bills.Board, now: datetime, *, edited: bool = False) -> Post | None:
    """Пост дня или `None`, если выставлять нечего."""
    overdue = board.of(bills.OVERDUE)
    soon = board.of(bills.SOON)
    if not overdue and not soon:
        return None
    lines: list[str] = []

    if overdue:
        count = len(overdue)
        lines.append(
            f"<b>Просрочено: {count} {plural(count, 'счёт', 'счёта', 'счетов')}"
            f" на {money(board.total(bills.OVERDUE))}</b>"
        )
        by_own: dict[str, list[Contract]] = {}
        for contract in contracts(overdue):
            by_own.setdefault(contract.own, []).append(contract)
        # Юрлицо, где просрочено больше, - выше: с него и начинать.
        firms = sorted(by_own.items(), key=lambda pair: (-sum(len(c.items) for c in pair[1]), pair[0]))
        for firm, items in firms:
            lines.append("")
            lines.append(f"<i>{_esc(firm or 'Без юрлица')}</i>")
            lines.extend(contract.line() for contract in items)
    if soon:
        lines.append("")
        lines.append(f"<b>В ближайшие дни: {len(soon)}</b>")
        lines.extend(_soon_line(bill) for bill in soon)

    # Не влезло - режем хвост просрочки, а не ближайшие: их день наступает
    # сейчас, а просрочка целиком есть в KORT.
    hidden = 0
    while len("\n".join(lines)) > LIMIT:
        soon_at = next((i for i, line in enumerate(lines) if line.startswith("<b>В ближайшие дни")), len(lines))
        cut = next((i for i in range(soon_at - 1, -1, -1) if lines[i].startswith("• ")), None)
        if cut is None:
            break
        del lines[cut]
        hidden += 1
        if cut >= 2 and lines[cut - 1].startswith("<i>") and (cut >= len(lines) or not lines[cut].startswith("• ")):
            del lines[cut - 2 : cut]  # юрлицо осталось без строк - снять и его заголовок
    if hidden:
        lines.append("")
        lines.append(f"И ещё {hidden} {plural(hidden, 'договор', 'договора', 'договоров')} - полный список в KORT")
    undated = len(board.of(bills.UNDATED))
    if undated:
        lines.append("")
        lines.append(f"Без срока в книге: {undated} - в KORT, «Счета → Не выставлены»")
    body = "\n".join(lines)

    local = now.astimezone(TZ)
    head = f"<b>Не выставлены счета</b> · {WEEKDAYS[local.weekday()]}, {local:%d.%m.%Y}"
    source = _esc(board.title or "книга-сводка")
    foot = f"<i>По книге «{source}»" + (f" · обновлено {local:%H:%M}" if edited else "") + "</i>"
    return Post(body=body, text=f"{head}\n\n{body}\n\n{foot}")


def keyboard() -> dict[str, Any] | None:
    link = section_link()
    if not link:
        return None
    return {"inline_keyboard": [[{"text": "Открыть в KORT", "url": link}]]}


# ── Один пост на день ────────────────────────────────────────────────────────


def _ensure(workspace_id: uuid.UUID) -> None:
    for name in (DAY, POST, HASH):
        try:
            with finance_session() as session:
                if session.get(Counter, (workspace_id, name)) is None:
                    session.add(Counter(workspace_id=workspace_id, name=name, value=0))
        except IntegrityError:
            pass  # завёл соседний процесс


def _value(workspace_id: uuid.UUID, name: str) -> int:
    with finance_session() as session:
        row = session.get(Counter, (workspace_id, name))
        return int(row.value) if row is not None else 0


def _set(workspace_id: uuid.UUID, name: str, value: int) -> None:
    with finance_session() as session:
        session.execute(
            sa.update(Counter).where(Counter.workspace_id == workspace_id, Counter.name == name).values(value=value)
        )


def day_of(now: datetime) -> int:
    local = now.astimezone(TZ)
    return local.year * 10000 + local.month * 100 + local.day


def claim(workspace_id: uuid.UUID, day: int) -> int | None:
    """Сводка дня - этому процессу. Вернёт прежний день или `None`, если её уже взяли.

    `UPDATE … WHERE value < day` под блокировкой строки: второй процесс дождётся
    первого и увидит, что день уже не меньше, - и ничего не выложит.
    """
    _ensure(workspace_id)
    with finance_session() as session:
        previous = session.scalar(
            sa.select(Counter.value).where(Counter.workspace_id == workspace_id, Counter.name == DAY)
        )
        result = session.execute(
            sa.update(Counter)
            .where(Counter.workspace_id == workspace_id, Counter.name == DAY, Counter.value < day)
            .values(value=day)
        )
        return int(previous or 0) if (result.rowcount or 0) == 1 else None


def release(workspace_id: uuid.UUID, day: int, previous: int) -> None:
    """Telegram не принял - день снова свободен, следующий проход повторит."""
    with finance_session() as session:
        session.execute(
            sa.update(Counter)
            .where(Counter.workspace_id == workspace_id, Counter.name == DAY, Counter.value == day)
            .values(value=previous)
        )


def due(now: datetime) -> bool:
    """Пора выкладывать: будний день, от часа сводки до вечера."""
    hour = finance_settings.bills_hour_value
    local = now.astimezone(TZ)
    return hour is not None and local.weekday() < 5 and hour <= local.hour < LAST_HOUR


def target_workspace() -> uuid.UUID | None:
    """Чью книгу читает бот: `FINANCE_BILLS_WORKSPACE_ID`, иначе единственная подключённая.

    Две компании со сводкой и без переменной - не выбор наугад, а отказ:
    в канал BBC не должна уйти чужая книга.
    """
    raw = _clean(finance_settings.bills_workspace_id)
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            log.error("счета: FINANCE_BILLS_WORKSPACE_ID - не id компании: %s", raw)
            return None
    with finance_session() as session:
        ids = list(session.scalars(sa.select(SummarySource.workspace_id)))
    if len(ids) == 1:
        return ids[0]
    if ids:
        log.error("счета: книг-сводок %s - задайте FINANCE_BILLS_WORKSPACE_ID", len(ids))
    return None


def tick(api: Bot, chat: str, now: datetime | None = None) -> str:
    """Один проход: выложить сводку дня, если пора, или поправить выложенную.

    Возвращает, что сделано: `posted`, `edited`, `empty`, `same`, `wait` и т. п.
    """
    now = now or datetime.now(timezone.utc)
    workspace_id = target_workspace()
    if workspace_id is None:
        return "no-workspace"
    with finance_session() as session:
        slot = bills.get(session, workspace_id)
    board = slot.board if slot is not None else None
    if board is None:
        return "no-board"
    day = day_of(now)
    if due(now):
        previous = claim(workspace_id, day)
        if previous is not None:
            post = render(board, now)
            if post is None:
                _set(workspace_id, POST, 0)
                return "empty"
            try:
                sent = api.call(
                    "sendMessage",
                    {
                        "chat_id": chat,
                        "text": post.text,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard(),
                        "link_preview_options": {"is_disabled": True},
                    },
                )
            except TelegramError:
                release(workspace_id, day, previous)
                raise
            _set(workspace_id, POST, int(sent["message_id"]))
            _set(workspace_id, HASH, post.digest)
            return "posted"
    if _value(workspace_id, DAY) != day:
        return "wait"
    post_id = _value(workspace_id, POST)
    if not post_id:
        return "wait"
    post = render(board, now, edited=True)
    if post is None:
        post = Post(body="", text=_cleared(board, now))
    if post.digest == _value(workspace_id, HASH):
        return "same"
    try:
        api.call(
            "editMessageText",
            {
                "chat_id": chat,
                "message_id": post_id,
                "text": post.text,
                "parse_mode": "HTML",
                "reply_markup": keyboard(),
                "link_preview_options": {"is_disabled": True},
            },
        )
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            raise
    _set(workspace_id, HASH, post.digest)
    return "edited"


def _cleared(board: bills.Board, now: datetime) -> str:
    """Всё из поста выставили за день - пост так и говорит, а не висит списком."""
    local = now.astimezone(TZ)
    return (
        f"<b>Не выставлены счета</b> · {WEEKDAYS[local.weekday()]}, {local:%d.%m.%Y}\n\n"
        "Всё из этого списка выставлено.\n\n"
        f"<i>По книге «{_esc(board.title or 'книга-сводка')}» · обновлено {local:%H:%M}</i>"
    )


# ── Канал для «Счетов» ───────────────────────────────────────────────────────

_channel: dict[str, Any] = {}
_channel_lock = threading.Lock()


def _remember_channel(api: Bot, chat: str) -> None:
    try:
        info = api.call("getChat", {"chat_id": chat}) or {}
    except TelegramError as exc:
        log.warning("счета: канал %s не прочитан (%s)", chat, exc)
        return
    username = info.get("username") or ""
    with _channel_lock:
        _channel.update(
            title=str(info.get("title") or ""),
            url=f"https://t.me/{username}" if username else str(info.get("invite_link") or ""),
            at=time.monotonic(),
        )


def channel_out(workspace_id: uuid.UUID) -> dict[str, Any] | None:
    """Канал бота - для строки в «Счетах», если бот читает книгу этой компании."""
    if not finance_settings.bills_ready:
        return None
    try:
        if target_workspace() != workspace_id:
            return None
    except Exception:  # noqa: BLE001 - строка о канале не роняет раздел
        return None
    with _channel_lock:
        title, url = _channel.get("title", ""), _channel.get("url", "")
    chat = _clean(finance_settings.bills_chat_id)
    if not url and chat.startswith("@"):
        url = f"https://t.me/{chat[1:]}"
    return {"title": title or chat, "url": url, "hour": finance_settings.bills_hour_value}


# ── Фоновый цикл ─────────────────────────────────────────────────────────────


async def run_forever() -> None:
    """Фоновая задача API: сводка дня и её правка, пока приложение живо."""
    if not finance_settings.bills_ready:
        return
    api = Bot(_clean(finance_settings.bills_bot_token))
    chat = _clean(finance_settings.bills_chat_id)
    log.info("счета: бот запущен, канал %s", chat)
    await asyncio.sleep(30)
    while True:
        try:
            with _channel_lock:
                stale = time.monotonic() - _channel.get("at", -CHANNEL_TTL) >= CHANNEL_TTL
            if stale:
                await asyncio.to_thread(_remember_channel, api, chat)
            done = await asyncio.to_thread(tick, api, chat)
            if done in ("posted", "edited"):
                log.info("счета: %s", done)
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            log.warning("счета: Telegram не принял (%s)", exc)
        except Exception:  # noqa: BLE001 - фоновый проход не роняет приложение
            log.exception("счета: проход упал")
        await asyncio.sleep(TICK)


__all__ = [
    "LIMIT",
    "channel_out",
    "claim",
    "contracts",
    "due",
    "months_text",
    "release",
    "render",
    "run_forever",
    "target_workspace",
    "tick",
]
