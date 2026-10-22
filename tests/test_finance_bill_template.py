"""Шаблон счетов Excel (`contracts/bill_template.py`): скачать в разделе «Счета», заполнить, загрузить.

Набор держит обещания шаблона:
* колонки и порядок - как в шаблоне финансистов BBC (22.10.2026), строка на
  счёт, известное вписано, «№ Счета» и «Дата выстав. Счета» - жёлтые и пустые;
* загруженный шаблон разбирается по названиям колонок, строка счёта - по
  скрытому коду KORT или по юрлицу, клиенту, № договора и «Периоду с»; шаблон
  финансистов без кода тоже подходит;
* каждая строка - со своим состоянием: записать, уже в книге, не заполнена,
  отказ словами; отказ одной строки не роняет остальные;
* не нашлась, нашлось две, счёт в файле дважды, правили договор, нет номера
  или даты, в книге уже другой номер - отказ строке, а не догадка;
* пустые «Счет за Период» и «Доп. Коммент.» не стирают книгу; «Счет за
  Период» при выставлении подставляется, как в карточке.
"""
from __future__ import annotations

import dataclasses
import io
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from app.finance.contracts import bill_template
from app.finance.contracts.bills import DONE, Bill, Board

AYA = Bill(
    line=10, group="overdue", text="Просрочка 2–5 дней", own="BBCS", customer="ИП АЙЯ", number="№НО/39",
    month="ОКТЯБРЬ 2026", month_no=10, kind="Абон.П.", staff="Ануар", department="НО",
    amount=Decimal("91000"), period_from=date(2026, 10, 20), period_to=date(2026, 11, 19), paid="", status="",
    note="", onec_name="ИП Айя", signed_at="20.01.2026",
)
RAJA = dataclasses.replace(AYA, line=11, customer="ИП РАЖА", number="№НО/37", amount=Decimal("78000"), onec_name="ИП Ража")
KADYR = dataclasses.replace(
    AYA, line=12, customer="ИП Кадыркулова Торгын Карашевна", number="№НО/19", staff="Дана", amount=Decimal("25000"),
    period_from=date(2026, 10, 17), period_to=date(2026, 11, 16), onec_name="ИП Кадыркулова", signed_at="17.01.2026",
)
KENESARY = dataclasses.replace(
    AYA, line=13, own="BBCL", customer="ТОО KENESARY AGRO", number="№ЮО/44", amount=Decimal("150000"),
    period_from=date(2026, 10, 22), period_to=date(2026, 11, 21), onec_name="", signed_at="",
)
#: Тот же договор АЙЯ за сентябрь - выставлен: номер 248.
AYA_SEPTEMBER = dataclasses.replace(
    AYA, line=5, group=DONE, text="TRUE", month="СЕНТЯБРЬ 2026", period_from=date(2026, 9, 20),
    period_to=date(2026, 10, 19), bill_no="248", bill_date="21.09.2026",
)


def board(*done: Bill) -> Board:
    return Board(
        read_at=datetime(2026, 10, 22, tzinfo=timezone.utc), title="Сводка", worksheet="Сводка", spreadsheet_id="s",
        bills=[KADYR, AYA, RAJA, KENESARY], done=[AYA_SEPTEMBER, *done],
    )


def workbook(data: bytes):
    return load_workbook(io.BytesIO(data))


def saved(book) -> bytes:
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def filled(data: bytes, cells: dict[str, object]) -> bytes:
    book = workbook(data)
    for address, value in cells.items():
        book.active[address].value = value
    return saved(book)


def statuses(result: dict) -> list[tuple[str, str]]:
    return [(row["customer"], row["status"]) for row in result["rows"]]


def test_shablon_kak_u_finansistov() -> None:
    sheet = workbook(bill_template.build([AYA, RAJA, KENESARY])).active
    assert sheet.title == "Счета"
    heads = {cell.column_letter: cell.value for cell in sheet[1] if cell.value}
    assert heads == {
        "B": "Наша\nФирма", "C": "Наш\nСотрудник", "D": "Заказчик\n(Название Фирмы)", "E": "По 1C",
        "F": "№\nДоговора", "G": "Дата заключения \nДоговора", "I": "Период\nс (нач.)", "J": "Период\nпо (зав.)",
        "L": "Сумма\nДоговора", "N": "№ \nСчета", "O": "Дата выстав.\nСчета", "Q": "Код KORT",
    }
    row = {cell.column_letter: cell.value for cell in sheet[2] if cell.value is not None}
    assert row["B"] == "BBCS" and row["C"] == "Ануар" and row["D"] == "ИП АЙЯ" and row["E"] == "ИП Айя"
    assert row["F"] == "№НО/39" and row["L"] == 91000
    assert row["G"].date() == date(2026, 1, 20) and row["I"].date() == date(2026, 10, 20)
    assert row["J"].date() == date(2026, 11, 19)
    assert "N" not in row and "O" not in row, "номер и дату заполняет человек"
    assert [sheet[f"D{n}"].value for n in (2, 3, 4)] == ["ИП АЙЯ", "ИП РАЖА", "ТОО KENESARY AGRO"], "строка на счёт, по порядку"
    assert [sheet[f"Q{n}"].value for n in (2, 3, 4)] == [AYA.id, RAJA.id, KENESARY.id]
    assert sheet.column_dimensions["Q"].hidden, "код счёта - в скрытой колонке"
    assert sheet["E4"].value is None, "«По 1C» пусто - пусто, а не «None»"
    for address in ("N1", "O1", "N4", "O4"):
        assert sheet[address].fill.fgColor.rgb == "FFFFF2CC", f"{address}: жёлтое, не прозрачное"
    assert sheet["O3"].number_format == "DD.MM.YYYY" and sheet["L2"].number_format == "#,##0"
    [rule] = sheet.data_validations.dataValidation
    assert str(rule.sqref) == "O2:O4", "дата проверяется у каждой строки"
    assert sheet.freeze_panes == "A2"
    assert bill_template.filename(date(2026, 10, 22)) == "Счета к выставлению 22.10.2026.xlsx"


def test_zapolnennyy_shablon_v_stroki() -> None:
    data = filled(
        bill_template.build([AYA, RAJA, KENESARY]),
        {"N2": 312, "O2": datetime(2026, 10, 22), "N4": "248/1", "O4": "22.10.2026"},
    )
    result = bill_template.parse(data, board())
    assert statuses(result) == [("ИП АЙЯ", "ready"), ("ИП РАЖА", "empty"), ("ТОО KENESARY AGRO", "ready")]
    aya, raja, kenesary = result["rows"]
    assert aya["id"] == AYA.id and aya["sheet"] == "Счета" and aya["row"] == 2
    assert aya["values"] == {"bill_no": "312", "bill_date": "2026-10-22"}
    assert aya["defaults"] == {"bill_period": "с 20.10.2026 года по 19.11.2026 года"}, "как подставляет карточка"
    assert aya["own"] == "BBCS" and aya["period_from"] == "2026-10-20" and aya["amount"] == "91000"
    assert raja["values"] == {} and raja["problem"] == "", "не заполнена - не ошибка"
    assert kenesary["values"] == {"bill_no": "248/1", "bill_date": "2026-10-22"}, "номер и дата текстом - тоже"
    assert result["counts"] == {"ready": 2, "same": 0, "empty": 1, "error": 0}


def finance_template(rows: list[list]) -> bytes:
    """Шаблон в точности как прислали финансисты 22.10.2026: шапка в 1-й строке, без кода KORT."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Лист1"
    heads = {
        "B": "Наша\nФирма", "C": "Наш\nСотрудник", "D": "Заказчик\n(Название Фирмы)", "E": "По 1C",
        "F": "№\nДоговора", "G": "Дата заключения \nДоговора", "I": "Период\nс (нач.)", "J": "Период\nпо (зав.)",
        "L": "Сумма\nДоговора", "N": "№ \nСчета", "O": "Дата выстав.\nСчета",
    }
    for letter, title in heads.items():
        sheet[f"{letter}1"] = title
    for number, values in enumerate(rows, start=2):
        for letter, value in zip(("B", "C", "D", "E", "F", "G", "I", "J", "L", "N", "O"), values):
            sheet[f"{letter}{number}"] = value
    return saved(book)


THREE = [
    ["BBCS", "Дана", "ИП Кадыркулова Торгын Карашевна", "ИП Кадыркулова", "№НО/19", datetime(2026, 1, 17),
     datetime(2026, 10, 17), datetime(2026, 11, 16), "25\xa0000", 501, datetime(2026, 10, 22)],
    ["BBCS", "Ануар", "ИП АЙЯ", "ИП Айя", "№НО/39", datetime(2026, 1, 20),
     datetime(2026, 10, 20), datetime(2026, 11, 19), "91\xa0000", 502, datetime(2026, 10, 22)],
    ["BBCS", "Ануар", "ИП РАЖА", "ИП Ража", "№НО/37", datetime(2026, 1, 20),
     datetime(2026, 10, 20), datetime(2026, 11, 19), "78\xa0000", None, None],
]


def test_shablon_finansistov_bez_koda() -> None:
    """Их файл: строки - по юрлицу, клиенту, № договора и «Периоду с»; сентябрь АЙЯ не путается с октябрём."""
    result = bill_template.parse(finance_template(THREE), board())
    assert statuses(result) == [
        ("ИП Кадыркулова Торгын Карашевна", "ready"), ("ИП АЙЯ", "ready"), ("ИП РАЖА", "empty"),
    ]
    kadyr, aya, _ = result["rows"]
    assert kadyr["id"] == KADYR.id and kadyr["values"] == {"bill_no": "501", "bill_date": "2026-10-22"}
    assert aya["id"] == AYA.id and aya["row"] == 3


def test_ne_nashlas_i_neskolko_mesyatsev() -> None:
    stranger = ["BBCL", "", "ТОО Неизвестный", "", "№ЮО/99", None, datetime(2026, 10, 22), None, "1", 9, datetime(2026, 10, 22)]
    result = bill_template.parse(finance_template([stranger, THREE[2]]), board())
    assert statuses(result) == [("ТОО Неизвестный", "error"), ("ИП РАЖА", "empty")]
    assert result["rows"][0]["problem"] == "В книге-сводке нет счёта BBCL, ТОО Неизвестный, №ЮО/99 с периодом от 22.10.2026"
    # Без «Периода с»: у АЙЯ два месяца (сентябрь выставлен, октябрь нет) - не угадывается.
    no_period = [*THREE[1][:6], None, None, *THREE[1][8:]]
    result = bill_template.parse(finance_template([no_period]), board())
    assert result["rows"][0]["status"] == "error"
    assert "несколько месяцев - впишите «Период с»" in result["rows"][0]["problem"]


def test_dvazhdy_v_fayle_otkaz_obeim() -> None:
    result = bill_template.parse(finance_template([THREE[1], THREE[0], THREE[1]]), board())
    assert statuses(result) == [("ИП АЙЯ", "error"), ("ИП Кадыркулова Торгын Карашевна", "ready"), ("ИП АЙЯ", "error")]
    assert "несколько раз (строки 2, 4)" in result["rows"][0]["problem"]
    # Незаполненный двойник не мешает заполненному.
    blank_twin = [*THREE[1][:9], None, None]
    result = bill_template.parse(finance_template([THREE[1], blank_twin]), board())
    assert statuses(result) == [("ИП АЙЯ", "ready"), ("ИП АЙЯ", "empty")]


def test_pravili_dogovor_v_shablone() -> None:
    book = workbook(filled(bill_template.build([AYA, KENESARY]), {"N2": 312, "O2": datetime(2026, 10, 22)}))
    book.active["D2"].value = "ИП Другой"
    result = bill_template.parse(saved(book), board())
    assert result["rows"][0]["status"] == "error" and "Строку правили" in result["rows"][0]["problem"]
    assert result["rows"][1]["status"] == "empty", "остальные строки разбираются"


def test_net_nomera_net_daty_plokhaya_data() -> None:
    data = filled(
        bill_template.build([AYA, RAJA, KENESARY, KADYR]),
        {"N2": 312, "O3": datetime(2026, 10, 22), "N4": 7, "O4": "завтра", "N5": "+312", "O5": datetime(2026, 10, 22)},
    )
    rows = bill_template.parse(data, board())["rows"]
    assert [row["status"] for row in rows] == ["error"] * 4
    assert [row["problem"] for row in rows] == [
        "«Дата выстав. Счета» - пусто",
        "Не вписан «№ Счета»",
        "«Дата выстав. Счета» - не дата: «завтра»",
        "«№ Счета» не может начинаться с =, + или @",
    ]
    assert all(row["values"] == {} for row in rows), "отказанной строке нечего записывать"


def test_uzhe_v_knige() -> None:
    """Выставленный счёт: тот же номер - «уже в книге»; другой - отказ, а не перезапись."""
    template = bill_template.build([AYA])
    same = bill_template.parse(filled(template, {"N2": 248, "O2": datetime(2026, 9, 21), "Q2": AYA_SEPTEMBER.id}), board())
    [row] = same["rows"]
    assert (row["status"], row["note"], row["values"]) == ("same", "В книге уже № 248 от 21.09.2026", {})
    other = bill_template.parse(filled(template, {"N2": 300, "O2": datetime(2026, 9, 21), "Q2": AYA_SEPTEMBER.id}), board())
    [row] = other["rows"]
    assert row["status"] == "error" and row["problem"].startswith("В книге уже № 248 от 21.09.2026 - KORT его не перезаписывает")


def test_ne_xlsx_otkaz() -> None:
    with pytest.raises(bill_template.TemplateError, match="не файл .xlsx"):
        bill_template.parse(b"%PDF-1.4 not a workbook", board())
    with pytest.raises(bill_template.TemplateError, match="пустой"):
        bill_template.parse(b"", board())
    with pytest.raises(bill_template.TemplateError, match="больше 2 МБ"):
        bill_template.parse(b"PK" + b"0" * (bill_template.MAX_BYTES + 1), board())
    plain = Workbook()
    plain.active["A1"] = "просто таблица"
    with pytest.raises(bill_template.TemplateError, match="нет шапки шаблона"):
        bill_template.parse(saved(plain), board())
    with pytest.raises(bill_template.TemplateError, match="нет ни одной строки"):
        bill_template.parse(bill_template.build([]), board())


def test_kolonki_po_nazvaniyam_i_dop_polya() -> None:
    """Вставили колонку и добавили «Счет за Период» и «Доп. Коммент.»: читается по названиям; пустое не стирает."""
    book = workbook(filled(bill_template.build([AYA, KENESARY]), {"N2": 312, "O2": datetime(2026, 10, 22), "N3": 5, "O3": "22.10.2026"}))
    sheet = book.active
    sheet.insert_cols(1)
    sheet["S1"], sheet["S2"] = "Счет за\nПериод (Коммент)", "за октябрь"
    sheet["T1"], sheet["T2"], sheet["T3"] = "Доп. \nКоммент.", None, "оплатят частями"
    aya, kenesary = bill_template.parse(saved(book), board())["rows"]
    assert aya["values"] == {"bill_no": "312", "bill_date": "2026-10-22", "bill_period": "за октябрь"}
    assert aya["defaults"] == {}, "период вписан в файле - подставлять нечего"
    assert kenesary["values"] == {"bill_no": "5", "bill_date": "2026-10-22", "note": "оплатят частями"}
    assert kenesary["defaults"] == {"bill_period": "с 22.10.2026 года по 21.11.2026 года"}
