"""«Счета → Не выставлены» и бот счетов: книга-сводка → KORT и канал Telegram.

Google и Telegram подменены: проверяется разбор книги и что бот шлёт, а не
сеть. Набор держит обещания раздела и бота:

* колонка «Счет» находится по названию рядом с «№ Счета», «Счет за Период» и
  «Счет2» и после вставленной колонки; состояние в KORT - словами книги,
  незнакомое - в «Без срока», а не угадано;
* пост - по шаблону заказчика v1.3: день счёта по «Периоду с», галочка «Счет»
  убирает счёт, абонентская плата и аренда раздельно, «Разовый» и «Прочее» не
  идут; вчерашнее и просроченное - с пометкой; без вида и без даты - названы
  строкой, а не потеряны; длинный пост режется с самых старых;
* пост по часам: второй проход и второй процесс не выкладывают его снова,
  отказ Telegram освобождает час, галочка правит пост, всё выставлено - пост
  удалён;
* новое - сразу и со звуком, но не чаще `NEW_GAP`; кнопка «Прислать сейчас» -
  без звука и не чаще `PULL_GAP`; `/bind` ставит кнопку заново;
* первый запуск новой редакции правит висящий пост, а не выдаёт весь список
  новым;
* раздел открыт только с правом «Счета», без подключённой сводки - пусто.
"""
from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.finance import auth, bills_bot
from app.finance.config import finance_settings
from app.finance.contracts import bills
from app.finance.contracts.models import BillNotice
from app.finance.db import finance_session
from app.finance.support_bot import TelegramError

BASE = "/api/v1/finance"
PASSWORD = "pass-12345"
CHAT = "@bbc_bills"

HEADER = [
    "Техн. 2Наша \nФирма",
    "Мес",
    "Техн. 1",
    "Наша \nФирма",
    "Вид \nУслуги",
    "Наш \nСотрудник",
    "Заказчик\n(Название Фирмы)",
    "Сумма\nДоговора",
    "Отдел",
    "№\nДоговора",
    "Период\nс (нач.)",
    "Период\nпо (зав.)",
    "Доп. \nКоммент.",
    "Счет",
    "Счет за\nПериод (Коммент)",
    "№ \nСчета",
    "Факт Оплата",
    "Сумма Факт\nПоступ.",
    "Статус\n(Продл./Приостн./Разв./На исп.)",
    "Счет2",
]
BILL = HEADER.index("Счет")


def until_of(period: str) -> str:
    """«22.10.2026» → «21.11.2026»: месяц договора, как в книге."""
    if not period:
        return ""
    start = datetime.strptime(period, "%d.%m.%Y").date()
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return (date(year, month, start.day) - timedelta(days=1)).strftime("%d.%m.%Y")


def row(
    month_no: int,
    month: str,
    own: str,
    customer: str,
    number: str,
    bill: str,
    *,
    kind: str = "Абон.П.",
    department: str = "ОБО",
    amount: str = "100 000",
    period: str = "",
    staff: str = "Меруерт",
    paid: str = "FALSE",
) -> list[str]:
    return [
        "", str(month_no), month, own, kind, staff, customer, amount, department, number,
        period, until_of(period), "", bill, "", "", paid, "0", "", bill,
    ]


def rent(month_no: int, month: str, customer: str, number: str, bill: str, **extra: str) -> list[str]:
    extra.setdefault("kind", "Аренда")
    extra.setdefault("department", "Аренда")
    extra.setdefault("amount", "50 000")
    return row(month_no, month, "BBC Omar", customer, number, bill, staff='ТОО "BBC Omar"', **extra)


OCT = "ОКТЯБРЬ 2026"
SHEET = [
    HEADER,
    rent(6, "ИЮНЬ 2026", "ТОО Молфуд", "№RENT/93", "Просрочка более 5 дней", period="04.06.2026"),
    rent(7, "ИЮЛЬ 2026", "ТОО Молфуд", "№RENT/93", "Просрочка более 5 дней", period="04.07.2026"),
    rent(8, "АВГУСТ 2026", "ТОО Молфуд", "№RENT/93", "Просрочка более 5 дней", period="04.08.2026"),
    rent(10, OCT, "ТОО Молфуд", "№RENT/93", "Просрочка более 5 дней", period="04.10.2026"),
    row(10, OCT, "BBC", "ADK Home", "№ОБО/110", "Просрочка 2–5 дней", kind="нет", period="17.10.2026", paid="Часть"),
    row(10, OCT, "BBC", "Алатау Пауэр", "№ОБО/146", "TRUE", period="25.09.2026"),
    row(10, OCT, "BBCL", "TrioSynergy", "№ЮО/58", "3 дня до выставления", department="ЮО", period="24.10.2026"),
    row(10, OCT, "BBCL", "Meliora", "№ЮО/35-2", "Еще рано", department="ЮО", period="26.10.2026"),
    row(8, "АВГУСТ 2026", "BBCL", "TrioSynergy", "№ЮО/136", "", kind="нет", department="ЮО"),
    row(5, "Старые (до Май/Июня 2026)", "BBCS", "Интер Табыс", "№НО/108", "FALSE", kind="нет", department="НО",
        period="15.05.2026"),
    row(10, OCT, "BBCL", "ТОО KENESARY AGRO", "№ЮО/44", "Сегодня день выставления", department="ЮО",
        amount="150 000", period="22.10.2026"),
    rent(10, OCT, "ТОО Custom Trade", "№RENT/84", "Просрочка 1 день", kind="нет", amount="0", period="21.10.2026"),
    row(10, OCT, "BBCS", "ИП АЙЯ", "№НО/39", "Просрочка 2–5 дней", department="НО", amount="91 000",
        period="20.10.2026"),
    row(10, OCT, "EA", "ТОРИ КОМПАНИ", "№НО/123", "Просрочка более 5 дней", kind="Разовый", department="НО",
        amount="500 000", period="15.10.2026"),
    rent(10, OCT, "ИП ЖАЛМҰХАН", "№ RENT/112", ""),
    rent(10, OCT, "ТОО Find Dream", "№RENT/94", "Сегодня день выставления", amount="", period="22.10.2026"),
    ["", "", "", "", "", "", "", "", "", "", "", "", "", "FALSE"],  # пустая строка листа - не счёт
]


def line_of(values: list[list[str]], customer: str, period: str = "") -> list[str]:
    """Строка книги по клиенту (и «Периоду с») - тест правит её, как человек правит книгу."""
    start = HEADER.index("Период\nс (нач.)")
    found = [line for line in values[1:] if customer in line and (not period or line[start] == period)]
    assert len(found) == 1, (customer, period, len(found))
    return found[0]


def test_sostoyanie_slovami_knigi() -> None:
    assert bills.state_of("TRUE") is None and bills.state_of("ИСТИНА") is None
    assert bills.state_of("Просрочка 1 день") == bills.OVERDUE
    assert bills.state_of("1 день до выставления") == bills.SOON
    assert bills.state_of("Сегодня день выставления") == bills.SOON, "день счёта - не «без срока»"
    assert bills.state_of("Ещё рано") == bills.EARLY
    for strange in ("", "FALSE", "Не нужен"):
        assert bills.state_of(strange) == bills.UNDATED, strange


def test_kniga_razbiraetsya_po_nazvaniyam() -> None:
    board = bills.build_board(SHEET, title="Осн.Общая сводка BBC 2026", worksheet="Сводка все ЮР лица")
    assert board.counts() == {bills.OVERDUE: 8, bills.SOON: 3, bills.EARLY: 1, bills.UNDATED: 3}
    assert board.rows == 16, "пустая строка листа - не договор"
    assert all(bill.customer != "Алатау Пауэр" for bill in board.bills), "выставленный счёт не в списке"
    first = board.bills[0]
    assert (first.customer, first.month, first.line) == ("ТОО Молфуд", "ИЮНЬ 2026", 2), "старое - первым"
    adk = next(bill for bill in board.bills if bill.customer == "ADK Home")
    assert adk.text == "Просрочка 2–5 дней" and adk.paid == "part" and adk.amount == Decimal("100000")
    assert adk.period_from == date(2026, 10, 17) and adk.period_to == date(2026, 11, 16)
    assert board.total(bills.OVERDUE) == Decimal("891000")
    assert {bill.text for bill in board.of(bills.UNDATED)} == {"", "FALSE"}

    # В книгу вставили колонку перед «Счет»: читается то же самое.
    shifted = [line[:5] + ["вставили"] + line[5:] for line in SHEET]
    again = bills.build_board(shifted, title="", worksheet="Сводка все ЮР лица")
    assert [bill.out()["text"] for bill in again.bills] == [bill.out()["text"] for bill in board.bills]
    assert "«Счет»" in again.drift


def test_bez_kolonki_schet_otkaz() -> None:
    broken = [[cell for cell in HEADER if cell != "Счет"]]
    with pytest.raises(bills.BillsError, match="Счет"):
        bills.build_board(broken, title="", worksheet="Сводка")


NOW = datetime(2026, 10, 22, 5, 30, tzinfo=timezone.utc)  # четверг, 10:30 в Астане


def board_of(values: list[list[str]]) -> bills.Board:
    return bills.build_board(values, title="Осн.Общая сводка BBC 2026", worksheet="Сводка")


def test_vid_uslugi() -> None:
    board = board_of(SHEET)
    kinds = {bill.customer: bills_bot.kind_of(bill) for bill in board.bills}
    assert kinds["ТОО KENESARY AGRO"] == bills_bot.ABON
    assert kinds["ТОО Find Dream"] == bills_bot.RENT
    assert kinds["ТОО Custom Trade"] == bills_bot.RENT, "вида нет - аренду узнаём по отделу"
    assert kinds["ТОРИ КОМПАНИ"] == bills_bot.SKIP, "разовые не присылаются"
    assert kinds["ADK Home"] == bills_bot.UNKNOWN, "вида нет и отдел не аренда - не угадываем"


POST = "\n".join(
    [
        "<b>Счета на оплату к выставлению: (22.10.2026)</b>",
        "<i>шаблон v1.3</i>",
        "-" * 55,
        "",
        "<b>по: BBCL</b> (22.10.2026 - 21.11.2026)",
        "",
        "1) <b>ТОО KENESARY AGRO</b> - №ЮО/44 = <b>150 000 ₸</b>",
        "",
        "Итого: <b>150 000 ₸</b>",
        "",
        "-" * 55,
        "",
        "<b>по: BBCS</b> - просрочено",
        "",
        "1) <b>ИП АЙЯ</b> - №НО/39 = <b>91 000 ₸</b> (с 20.10.2026)",
        "",
        "Итого: <b>91 000 ₸</b>",
        "",
        "-" * 55,
        "",
        "<b>Аренда - BBC Omar</b> (22.10.2026 - 21.11.2026)",
        "",
        "1) <b>ТОО Find Dream</b> - №RENT/94 = -",
        "",
        "Итого: <b>0 ₸</b>",
        "",
        "-" * 55,
        "",
        "<b>Аренда - BBC Omar</b> (21.10.2026 - 20.11.2026) - вчерашнее",
        "",
        "1) <b>ТОО Custom Trade</b> - №RENT/84 = <b>0 ₸</b>",
        "",
        "Итого: <b>0 ₸</b>",
        "",
        "-" * 55,
        "",
        "<b>Аренда - BBC Omar</b> - просрочено",
        "",
        "1) <b>ТОО Молфуд</b> - №RENT/93 = <b>50 000 ₸</b> (с 04.10.2026)",
        "",
        "Итого: <b>50 000 ₸</b>",
        "",
        "-" * 55,
        "<b>ВСЕГО: 5 счетов = 291 000 ₸</b>",
        "",
        "Старый хвост, раньше сентября: 3 счёта на 150 000 ₸ - список в KORT",
        "Без даты «Период с» в книге: 1 - ИП ЖАЛМҰХАН",
        "Без вида услуги в книге: 1 - ADK Home",
    ]
)


def test_post_po_shablonu() -> None:
    """Шаблон v1.3: абонентская плата, затем аренда; сегодня, вчера, раньше; итоги и ВСЕГО."""
    post = bills_bot.render(board_of(SHEET), NOW)
    assert post is not None
    assert post.text == POST
    assert "TrioSynergy" not in post.text and "Meliora" not in post.text, "день счёта ещё не наступил"
    assert "ТОРИ" not in post.text, "разовые не присылаются"
    assert "Алатау" not in post.text, "галочка «Счет» - выставлен"
    assert chr(0x2014) not in post.text, "длинного тире в посте нет (CLAUDE.md)"


def test_dlinnyy_post_rezhetsya_so_starogo() -> None:
    many = [HEADER] + [
        rent(10, OCT, f"ТОО Арендатор с длинным названием {index}", f"№RENT/{index}", "Просрочка",
             period=f"{index % 20 + 1:02d}.10.2026")
        for index in range(150)
    ] + [SHEET[11]]
    post = bills_bot.render(board_of(many), NOW)
    assert post is not None
    assert bills_bot._visible(post.text) <= 4096 and post.text.count("<b>") <= bills_bot.ENTITIES
    assert "ТОО KENESARY AGRO" in post.text, "сегодняшнее не режется"
    assert "<b>ВСЕГО: 151 счёт = 7 650 000 ₸</b>" in post.text, "итог - по всем, и по не вошедшим"
    assert "(с 20.10.2026)" in post.text and "(с 01.10.2026)" not in post.text, "уходят самые старые"
    hidden = int(post.text.split("и ещё ", 1)[1].split(" ", 1)[0])
    assert post.text.count("ТОО Арендатор с длинным названием") + hidden == 150


def test_pustoy_spisok_ne_vykladyvaetsya() -> None:
    nothing = board_of([HEADER, line_of(SHEET, "Алатау Пауэр"), line_of(SHEET, "Meliora")])
    assert bills_bot.render(nothing, NOW) is None
    tail = board_of([HEADER, line_of(SHEET, "ТОО Молфуд", "04.06.2026")])
    assert bills_bot.render(tail, NOW) is None, "один хвост - только первым постом дня и по кнопке"
    alone = bills_bot.render(tail, NOW, extras_alone=True)
    assert alone is not None and "Старый хвост, раньше сентября: 1 счёт на 50 000 ₸" in alone.text
    assert "ВСЕГО" not in alone.text


# ── Пост по часам ────────────────────────────────────────────────────────────


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: TelegramError | None = None
        self.refuse: dict[str, TelegramError] = {}
        self.next_id = 100

    def call(self, method: str, params: dict | None = None, **_: object) -> dict:
        if self.fail is not None:
            raise self.fail
        if method in self.refuse:
            raise self.refuse[method]
        self.calls.append((method, params or {}))
        if method == "sendMessage":
            self.next_id += 1  # номер - только у нового сообщения, как в Telegram
        return {"message_id": self.next_id}

    def sent(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]


@pytest.fixture
def finance_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.finance import db as finance_db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}", future=True)
    monkeypatch.setattr(finance_db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(finance_db_module, "_session_factory", None)
    monkeypatch.setattr(finance_db_module, "_initialized", False)
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(finance_settings, "support_bot_token", "")
    monkeypatch.setattr(finance_settings, "bills_bot_token", "test-token")
    monkeypatch.setattr(finance_settings, "bills_chat_id", CHAT)
    monkeypatch.setattr(finance_settings, "bills_workspace_id", "")
    monkeypatch.setattr(finance_settings, "bills_hour", "10")
    monkeypatch.setattr(finance_settings, "bills_every", "3")
    monkeypatch.setattr(finance_settings, "app_url", "https://kort.cards")
    auth._ATTEMPTS.clear()
    bills._slots.clear()
    yield engine
    bills._slots.clear()
    auth._ATTEMPTS.clear()


@pytest.fixture
def app(finance_db) -> FastAPI:
    from finance_routes import finance_app

    return finance_app()


def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"x-client-ip": "10.0.0.7", "user-agent": "pytest-agent"})


def register(app: FastAPI) -> TestClient:
    person = client(app)
    response = person.post(
        f"{BASE}/auth/register",
        json={"email": "owner@bbc.kz", "password": PASSWORD, "company": "BBC (тест)", "full_name": "Ермеков Нурболат"},
    )
    assert response.status_code == 201, response.text
    return person


@pytest.fixture
def book(monkeypatch):
    """Книга в памяти: тест правит `book["values"]` - «в книге поставили галочку»."""
    from app.finance.contracts import summary

    state = {"values": [list(line) for line in SHEET]}
    monkeypatch.setattr(
        summary, "_read",
        lambda spreadsheet_id, tab: summary.build_index(state["values"], title="Осн.Общая сводка BBC 2026", worksheet=tab),
    )
    monkeypatch.setattr(
        bills, "_read",
        lambda spreadsheet_id, tab: bills.build_board(
            state["values"], title="Осн.Общая сводка BBC 2026", worksheet=tab, spreadsheet_id=spreadsheet_id
        ),
    )
    return state


def connect(owner: TestClient) -> None:
    response = owner.post(
        f"{BASE}/contracts/setup/summary",
        json={"link": "https://docs.google.com/spreadsheets/d/abc123/edit", "worksheet": "Сводка все ЮР лица"},
    )
    assert response.status_code == 200, response.text


def fresh() -> None:
    """Книгу перечитать сразу: бот берёт прочитанную, свежая доедет к следующему проходу."""
    with bills._guard:
        slots = list(bills._slots.values())
    for slot in slots:
        board = bills._read(*slot.source)
        with bills._guard:
            slot.board, slot.at = board, time.monotonic()


def issue_all(values: list[list[str]]) -> None:
    for line in values[1:]:
        if len(line) > BILL:
            line[BILL] = "TRUE"


def test_post_po_chasu_i_pravka(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()

    assert bills_bot.tick(api, CHAT, NOW) == "posted"
    [post] = api.sent("sendMessage")
    assert post["chat_id"] == CHAT and post["parse_mode"] == "HTML"
    assert post["text"] == POST
    assert post["disable_notification"] is None, "по часу - со звуком"
    assert post["reply_markup"]["inline_keyboard"] == [
        [
            {"text": "Прислать сейчас", "callback_data": "bills:now"},
            {"text": "Открыть в KORT", "url": "https://kort.cards/?s=invoices"},
        ]
    ]

    # Второй проход и «второй процесс» (тот же час в базе) - не выкладывают снова.
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=1)) == "same"
    assert bills_bot.claim(bills_bot.target_workspace(), bills_bot.slot_of(NOW)) is None
    assert len(api.sent("sendMessage")) == 1 and not api.sent("editMessageText")

    # В книге поставили галочку - пост правится, нового нет.
    line_of(book["values"], "ТОО Молфуд", "04.10.2026")[BILL] = "TRUE"
    fresh()
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=5)) == "edited"
    [edit] = api.sent("editMessageText")
    assert edit["message_id"] == 101 and "ТОО Молфуд" not in edit["text"]
    assert edit["text"].endswith("\n\n<i>обновлено 10:35</i>")
    assert "<b>ВСЕГО: 4 счёта = 241 000 ₸</b>" in edit["text"]
    fresh()
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=6)) == "same"

    # Выставили всё - поста в канале больше нет.
    issue_all(book["values"])
    fresh()
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=7)) == "deleted"
    assert api.sent("deleteMessage") == [{"chat_id": CHAT, "message_id": 101}]
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=8)) == "wait"

    # Завтра - новый пост, и вчерашнее в нём - с пометкой.
    book["values"] = [list(line) for line in SHEET]
    fresh()
    assert bills_bot.tick(api, CHAT, NOW + timedelta(days=1)) == "posted"
    [_, tomorrow] = api.sent("sendMessage")
    assert tomorrow["text"].startswith("<b>Счета на оплату к выставлению: (23.10.2026)</b>")
    assert "<b>по: BBCL</b> (22.10.2026 - 21.11.2026) - вчерашнее" in tomorrow["text"]
    assert "<b>ТОО Custom Trade</b> - №RENT/84 = <b>0 ₸</b> (с 21.10.2026)" in tomorrow["text"]


def test_otkaz_telegram_osvobozhdaet_chas(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    api.fail = TelegramError("нет связи с Telegram")
    with pytest.raises(TelegramError):
        bills_bot.tick(api, CHAT, NOW)
    api.fail = None
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=1)) == "posted"


def test_post_kazhdyy_den_v_svoy_chas(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    early = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)  # 08:00 в Астане
    late = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)  # 21:00
    saturday = datetime(2026, 10, 24, 5, 0, tzinfo=timezone.utc)  # 10:00
    # Первый проход запоминает список молча: он придёт постом по часу, а не «новым».
    assert bills_bot.tick(api, CHAT, early) == "seeded"
    assert bills_bot.tick(api, CHAT, late) == "wait", "по часу после 20:00 не выходит"
    assert api.calls == []
    assert bills_bot.tick(api, CHAT, saturday) == "posted", "и в выходные"
    assert len(api.sent("sendMessage")) == 1


def test_napominanie_kazhdye_tri_chasa(app: FastAPI, book) -> None:
    """10, 13, 16, 19: новый пост звенит, прошлый удаляется; строка - что изменилось."""
    owner = register(app)
    connect(owner)
    api = FakeBot()
    morning = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)  # 10:00 в Астане
    assert bills_bot.tick(api, CHAT, morning) == "posted"
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=2, minutes=59)) == "same", "12:59 - ещё 10:00"

    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=3)) == "posted"
    second = api.sent("sendMessage")[-1]
    assert second["text"].startswith(
        "<b>Счета на оплату к выставлению: (22.10.2026)</b>\n<i>шаблон v1.3</i>\n<i>С 10:00 без изменений</i>\n"
    )
    assert api.sent("deleteMessage") == [{"chat_id": CHAT, "message_id": 101}], "прошлый пост - прочь"

    line_of(book["values"], "ИП АЙЯ")[BILL] = "TRUE"
    book["values"].append(
        row(10, OCT, "BBC Omar", "ИП АЛТЫНБЕКОВА", "№ОБО/92", "Сегодня день выставления", amount="800 000",
            period="22.10.2026")
    )
    fresh()
    # Пост по часу назвал АЛТЫНБЕКОВУ - отдельного «нового» о ней нет.
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=6)) == "posted"
    assert "<i>С 13:00: выставлено 1, добавилось 1</i>" in api.sent("sendMessage")[-1]["text"]
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=6, minutes=1)) == "same"

    late = morning + timedelta(hours=10)  # 20:00 - по часам больше нет
    posts = len(api.sent("sendMessage"))
    assert "posted" not in bills_bot.tick(api, CHAT, late)
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=14)) != "posted"
    assert len(api.sent("sendMessage")) == posts
    assert bills_bot.slot_of(morning + timedelta(hours=24)) == 2026102310
    assert bills_bot.tick(api, CHAT, morning + timedelta(hours=24)) == "posted"
    assert "<i>С 22.10 16:00" in api.sent("sendMessage")[-1]["text"], "вчерашнее - с датой"


NIGHT = datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc)  # 03:00 в Астане, четверг 22.10


def test_novoe_srazu_no_ne_chashche_pauzy(app: FastAPI, book, monkeypatch) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    assert bills_bot.tick(api, CHAT, NIGHT) == "seeded"

    # В книгу добавили счёт, чей день сегодня, - пост сразу, ночью и со звуком.
    book["values"].append(
        row(10, OCT, "BBCS", "ИП НУСУПБАЕВ", "№ОБО/100", "Сегодня день выставления", amount="150 000",
            period="22.10.2026")
    )
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=1)) == "new"
    [first] = api.sent("sendMessage")
    assert "<b>по: BBCS</b> (22.10.2026 - 21.11.2026)\n\n1) <b>ИП НУСУПБАЕВ</b> - №ОБО/100 = <b>150 000 ₸</b>" in (
        first["text"]
    )
    assert "ТОО KENESARY AGRO" in first["text"], "в посте - весь список, а не одно новое"
    assert first["disable_notification"] is None, "ночью - со звуком (решение владельца)"
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=2)) == "same"

    # Старый хвост «новым» не бывает.
    book["values"].append(rent(8, "АВГУСТ 2026", "ТОО Stroy City 2050", "№RENT/75", "Просрочка", period="01.08.2026"))
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=2, seconds=30)) == "edited", "строка хвоста - правкой"
    assert len(api.sent("sendMessage")) == 1

    # Ещё новое через минуту - правка, а не второй звонок.
    book["values"].append(
        row(10, OCT, "BBC Omar", "ТОО Green Line Customs", "№ОБО/56", "Сегодня день выставления", amount="50 000",
            period="22.10.2026")
    )
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=3)) == "edited"
    assert len(api.sent("sendMessage")) == 1 and "Green Line Customs" in api.sent("editMessageText")[-1]["text"]

    # Выставили - строка уходит; сняли галочку после паузы - счёт снова новый.
    monkeypatch.setattr(bills_bot, "NEW_GAP", 0.0)
    kenesary = line_of(book["values"], "ТОО KENESARY AGRO")
    kenesary[BILL] = "TRUE"
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=4)) == "edited"
    assert "KENESARY" not in api.sent("editMessageText")[-1]["text"]
    kenesary[BILL] = "Сегодня день выставления"
    fresh()
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=5)) == "new"
    assert "KENESARY" in api.sent("sendMessage")[-1]["text"]
    assert api.sent("deleteMessage") == [{"chat_id": CHAT, "message_id": 101}], "прошлый пост - прочь"

    # Полночь: наступил день следующих счетов - пост сразу, вчерашнее - с пометкой.
    book["values"].append(
        row(10, OCT, "BBCS", "ИП ПЕН Г.И.", "№НО/47", "1 день до выставления", department="НО", amount="34 000",
            period="23.10.2026")
    )
    fresh()
    midnight = datetime(2026, 10, 22, 19, 0, 30, tzinfo=timezone.utc)  # 00:00:30 в Астане, 23.10
    assert bills_bot.tick(api, CHAT, midnight - timedelta(minutes=1)) in ("same", "edited")
    assert bills_bot.tick(api, CHAT, midnight) == "new"
    night_post = api.sent("sendMessage")[-1]["text"]
    assert night_post.startswith("<b>Счета на оплату к выставлению: (23.10.2026)</b>")
    assert "<b>по: BBCS</b> (23.10.2026 - 22.11.2026)\n\n1) <b>ИП ПЕН Г.И.</b>" in night_post
    assert "<b>по: BBCL</b> (22.10.2026 - 21.11.2026) - вчерашнее" in night_post


def test_novoe_ne_teryaetsya_pri_otkaze_telegram(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    assert bills_bot.tick(api, CHAT, NIGHT) == "seeded"
    book["values"].append(
        row(10, OCT, "BBCS", "ИП НУСУПБАЕВ", "№ОБО/100", "Сегодня день выставления", period="22.10.2026")
    )
    fresh()
    api.fail = TelegramError("нет связи с Telegram")
    with pytest.raises(TelegramError):
        bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=1))
    api.fail = None
    assert bills_bot.tick(api, CHAT, NIGHT + timedelta(minutes=2)) == "new"
    assert "ИП НУСУПБАЕВ" in api.sent("sendMessage")[0]["text"]


def test_staryy_post_ne_udalit_pravitsya(app: FastAPI, book) -> None:
    """Telegram не удаляет сообщения старше 48 часов - тогда «Всё выставлено» текстом."""
    owner = register(app)
    connect(owner)
    api = FakeBot()
    assert bills_bot.tick(api, CHAT, NOW) == "posted"
    issue_all(book["values"])
    fresh()
    api.refuse["deleteMessage"] = TelegramError("Bad Request: message can't be deleted", code=400)
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=2)) == "edited"
    assert "Всё выставлено." in api.sent("editMessageText")[-1]["text"]
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=3)) == "same"


def test_pervyy_zapusk_novoy_redakcii(app: FastAPI, book) -> None:
    """На проде висит пост прежнего вида (22.10, 15:00): он правится в шаблон, нового «всего списка» нет."""
    owner = register(app)
    connect(owner)
    workspace_id = bills_bot.target_workspace()
    bills_bot._ensure(workspace_id)
    for name, value in ((bills_bot.DAY, 2026102216), (bills_bot.POST, 8), (bills_bot.HASH, 12345), (bills_bot.SEEDED, 1)):
        bills_bot._set(workspace_id, name, value)
    with finance_session() as session:
        for key, group in (("bbc|adk home|№обо/110|октябрь 2026", "overdue"), ("x", "soon")):
            session.add(BillNotice(workspace_id=workspace_id, key=key, bill_group=group, line=""))
    api = FakeBot()
    afternoon = datetime(2026, 10, 22, 11, 30, tzinfo=timezone.utc)  # 16:30 в Астане
    assert bills_bot.tick(api, CHAT, afternoon) == "seeded+edited"
    assert not api.sent("sendMessage")
    [edit] = api.sent("editMessageText")
    assert edit["message_id"] == 8 and edit["text"].startswith("<b>Счета на оплату к выставлению: (22.10.2026)</b>")
    with finance_session() as session:
        groups = set(session.scalars(sa.select(BillNotice.bill_group)))
    assert groups == {"due"}, "записи прежней редакции стёрты"
    assert bills_bot.tick(api, CHAT, afternoon + timedelta(minutes=1)) == "same"


# ── Кнопка «Прислать сейчас» и /bind ─────────────────────────────────────────


def test_knopka_prislat_seychas(app: FastAPI, book, monkeypatch) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    assert bills_bot.pull(api, CHAT, NOW) == "Готово - список внизу канала"
    [post] = api.sent("sendMessage")
    assert post["text"] == POST and post["disable_notification"] is True, "по кнопке - без звука"
    assert bills_bot.pull(api, CHAT, NOW) == "Список только что отправлен - он внизу канала"
    assert len(api.sent("sendMessage")) == 1

    monkeypatch.setattr(bills_bot, "PULL_GAP", 0.0)
    assert bills_bot.pull(api, CHAT, NOW + timedelta(minutes=1)) == "Готово - список внизу канала"
    assert api.sent("deleteMessage") == [{"chat_id": CHAT, "message_id": 101}], "прошлый пост - прочь"
    # Пост по кнопке не делает час: в 13:00 выйдет свой.
    assert bills_bot.tick(api, CHAT, NOW + timedelta(minutes=2)) == "posted"

    issue_all(book["values"])
    fresh()
    assert bills_bot.pull(api, CHAT, NOW + timedelta(minutes=3)) == "На сегодня все счета выставлены"
    assert api.sent("deleteMessage")[-1] == {"chat_id": CHAT, "message_id": 103}


def press(data: str = "bills:now", chat: dict | None = None) -> dict:
    where = chat or {"id": -1003554193304, "username": "bbc_bills", "type": "channel"}
    return {"update_id": 7, "callback_query": {"id": "q1", "data": data, "message": {"message_id": 5, "chat": where}}}


def test_knopka_cherez_telegram(app: FastAPI, book, monkeypatch) -> None:
    owner = register(app)
    connect(owner)
    monkeypatch.setattr(bills_bot, "_now", lambda: NOW)
    api = FakeBot()
    bills_bot.handle(api, CHAT, press())
    assert api.sent("answerCallbackQuery") == [{"callback_query_id": "q1", "text": "Готово - список внизу канала"}]
    assert api.sent("sendMessage")[0]["text"] == POST

    bills_bot.handle(api, CHAT, press(chat={"id": -100777, "type": "supergroup"}))
    assert api.sent("answerCallbackQuery")[-1]["text"] == "Кнопка работает только в канале счетов"
    bills_bot.handle(api, CHAT, press("другое"))
    assert api.sent("answerCallbackQuery")[-1]["text"] is None
    assert len(api.sent("sendMessage")) == 1


def test_bind_stavit_knopku(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    api = FakeBot()
    assert bills_bot.ensure_panel(api, CHAT)
    [panel] = api.sent("sendMessage")
    assert panel["text"].startswith("<b>Счета на оплату к выставлению</b>\nСписок приходит в 10:00, 13:00, 16:00, 19:00")
    assert panel["reply_markup"] == {"inline_keyboard": [[{"text": "Прислать сейчас", "callback_data": "bills:now"}]]}
    assert panel["disable_notification"] is True
    assert api.sent("pinChatMessage") == [{"chat_id": CHAT, "message_id": 101, "disable_notification": True}]

    # Перезапуск: кнопка на месте - правится, вторая не ставится.
    assert bills_bot.ensure_panel(api, CHAT)
    assert len(api.sent("sendMessage")) == 1 and api.sent("editMessageText")[-1]["message_id"] == 101

    # Её удалили из канала - бот при запуске ставит заново.
    api.refuse["editMessageText"] = TelegramError("Bad Request: message to edit not found", code=400)
    assert bills_bot.ensure_panel(api, CHAT)
    del api.refuse["editMessageText"]
    assert [item["message_id"] for item in api.sent("pinChatMessage")] == [101, 102]

    # /bind в канале: команда уходит, кнопка - заново, прежняя откреплена и удалена.
    where = {"id": -1003554193304, "username": "bbc_bills", "type": "channel"}
    bills_bot.handle(api, CHAT, {"update_id": 8, "channel_post": {"message_id": 50, "chat": where, "text": "/bind"}})
    assert api.sent("deleteMessage") == [{"chat_id": CHAT, "message_id": 50}, {"chat_id": CHAT, "message_id": 102}]
    assert api.sent("unpinChatMessage") == [{"chat_id": CHAT, "message_id": 102}]
    assert api.sent("pinChatMessage")[-1]["message_id"] == 103

    # Служебное «закреплено» о своей кнопке - убирается; чужие посты бот не трогает.
    bills_bot.handle(
        api, CHAT, {"update_id": 9, "channel_post": {"message_id": 51, "chat": where, "pinned_message": {"message_id": 103}}}
    )
    bills_bot.handle(api, CHAT, {"update_id": 10, "channel_post": {"message_id": 52, "chat": where, "text": "Привет"}})
    assert api.sent("deleteMessage")[-1] == {"chat_id": CHAT, "message_id": 51}
    assert len(api.sent("deleteMessage")) == 3


def test_pustoy_list_ne_znachit_vse_vystavleno() -> None:
    with pytest.raises(bills.BillsError, match="ещё собирается"):
        bills.build_board([HEADER], title="", worksheet="Сводка")


def test_nevernyy_chas_vyklyuchaet_tolko_bota(monkeypatch) -> None:
    """«24» в FINANCE_BILLS_HOUR роняло весь API на старте (Render, 22.10.2026)."""
    from app.finance.config import FinanceSettings

    monkeypatch.setenv("FINANCE_BILLS_BOT_TOKEN", "test-token")
    monkeypatch.setenv("FINANCE_BILLS_CHAT_ID", CHAT)
    for raw in ("24", "-1", "десять", ""):
        monkeypatch.setenv("FINANCE_BILLS_HOUR", raw)
        settings = FinanceSettings()
        assert settings.bills_hour_value is None and not settings.bills_ready, raw
    monkeypatch.setenv("FINANCE_BILLS_HOUR", '"9"')
    for raw in ("0", "25", "три"):
        monkeypatch.setenv("FINANCE_BILLS_EVERY", raw)
        assert not FinanceSettings().bills_ready, raw
    monkeypatch.setenv("FINANCE_BILLS_EVERY", "2")
    assert FinanceSettings().bills_hour_value == 9 and FinanceSettings().bills_ready


def test_bez_svodki_bot_molchit(app: FastAPI, book) -> None:
    register(app)
    assert bills_bot.tick(FakeBot(), CHAT, NOW) == "no-workspace"


def test_razdel_ne_vystavleny(app: FastAPI, book) -> None:
    owner = register(app)
    empty = owner.get(f"{BASE}/bills").json()
    assert empty == {"source": None, "items": [], "counts": None, "channel": None}
    connect(owner)
    data = owner.get(f"{BASE}/bills").json()
    assert data["counts"]["overdue"] == 8 and data["counts"]["overdue_total"] == "891000"
    assert data["source"]["title"] == "Осн.Общая сводка BBC 2026" and data["source"]["error"] == ""
    assert data["items"][0]["customer"] == "ТОО Молфуд" and data["items"][0]["period_from"] == "2026-06-04"
    assert data["items"][0]["period_to"] == "2026-07-03"
    assert data["channel"]["url"] == "https://t.me/bbc_bills" and data["channel"]["hours"] == [10, 13, 16, 19]
    me = owner.get(f"{BASE}/auth/me").json()
    assert me["bills"] == {"overdue": 8, "soon": 3}


def test_razdel_tolko_s_pravom_scheta(app: FastAPI, book) -> None:
    owner = register(app)
    connect(owner)
    card = owner.post(
        f"{BASE}/people/employees", json={"full_name": "Юрист Без Счетов", "phone": "+77010000001", "access": True}
    )
    assert card.status_code == 201, card.text
    lawyer = client(app)
    assert lawyer.post(f"{BASE}/auth/phone/start", json={"phone": "+77010000001"}).json() == {"step": "set_password"}
    done = lawyer.post(f"{BASE}/auth/phone/set-password", json={"phone": "+77010000001", "password": "secret-123"})
    assert done.status_code == 200, done.text
    assert lawyer.get(f"{BASE}/auth/me").json()["access"]["invoices"] == "none"
    assert lawyer.get(f"{BASE}/bills").status_code == 403
    assert lawyer.get(f"{BASE}/auth/me").json()["bills"] is None

    access = owner.put(f"{BASE}/access/employee/{card.json()['id']}", json={"changes": {"invoices": "view"}})
    assert access.status_code == 200, access.text
    assert lawyer.get(f"{BASE}/bills").status_code == 200
    assert lawyer.get(f"{BASE}/auth/me").json()["bills"] == {"overdue": 8, "soon": 3}


def test_kanal_tolko_dlya_svoey_kompanii(app: FastAPI, book, monkeypatch) -> None:
    owner = register(app)
    connect(owner)
    monkeypatch.setattr(finance_settings, "bills_workspace_id", str(uuid.uuid4()))
    assert owner.get(f"{BASE}/bills").json()["channel"] is None
