"""Шаблон счёта Excel: скачать из карточки, заполнить у себя, загрузить обратно.

Обходной путь для тех, кому удобнее Excel, - предложили финансисты BBC
(22.10.2026, их «Шаблон Ексель.xlsx»). Колонки и их порядок - как в их
шаблоне: «Наша Фирма», «Наш Сотрудник», «Заказчик», «По 1C», «№ Договора»,
«Дата заключения Договора», «Период с/по», «Сумма Договора» - и жёлтые
«№ Счета» и «Дата выстав. Счета», которые заполняет человек.

Загруженный шаблон в книгу сам не пишет: он заполняет поля карточки, а в
книгу их записывает та же «Записать в книгу» с теми же проверками
(`bill_book.save`). Человек видит, что вышло из файла, прежде чем запись уйдёт.

Разбор - по названиям колонок, а не по буквам: шаблон правят и вставляют в
него колонки. Строка счёта ищется по скрытому «Код KORT» и по юрлицу, клиенту
и № договора; шаблон финансистов на несколько счетов тоже подходит - из него
берётся строка этого счёта. Не нашлась или нашлось две - отказ, а не догадка.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from app.finance.contracts.bills import Bill, read_date
from app.finance.layout import Column, LayoutError, norm, resolve_layout

#: Сколько весит загружаемый шаблон: одна-две строки - это килобайты.
MAX_BYTES = 2 * 1024 * 1024
#: Сколько он весит распакованным: .xlsx - zip, и маленький файл может раскрыться в гигабайты.
MAX_UNPACKED = 50 * 1024 * 1024
#: Сколько строк просматривать под шапкой и где искать саму шапку.
MAX_ROWS = 1000
HEADER_SEARCH = 15
EPOCH = date(1899, 12, 30)

#: Жёлтые колонки - их заполняет человек (как в шаблоне финансистов).
#: Цвет - с непрозрачной альфой (FF…), как в файле финансистов: «FFF2CC»
#: openpyxl пишет как 00FFF2CC, и LibreOffice показал бы клетку без заливки.
FILL = PatternFill(fill_type="solid", fgColor="FFFFF2CC")
DATE_FORMAT = "DD.MM.YYYY"

#: Колонки шаблона: буква, заголовок, ширина - как у финансистов; `field` -
#: что из карточки туда кладётся.
LAYOUT: tuple[tuple[str, str, float, str], ...] = (
    ("B", "Наша\nФирма", 9.3, "own"),
    ("C", "Наш\nСотрудник", 13.0, "staff"),
    ("D", "Заказчик\n(Название Фирмы)", 24.4, "customer"),
    ("E", "По 1C", 22.0, "onec_name"),
    ("F", "№\nДоговора", 10.6, "number"),
    ("G", "Дата заключения \nДоговора", 17.9, "signed_at"),
    ("I", "Период\nс (нач.)", 10.6, "period_from"),
    ("J", "Период\nпо (зав.)", 10.6, "period_to"),
    ("L", "Сумма\nДоговора", 14.6, "amount"),
    ("N", "№ \nСчета", 12.0, "bill_no"),
    ("O", "Дата выстав.\nСчета", 16.3, "bill_date"),
)
#: Узкие колонки-просветы между блоками - как в шаблоне финансистов.
GAPS = {"A": 2.7, "H": 4.0, "K": 3.0, "M": 2.9}
#: Скрытая колонка - по ней загруженный шаблон узнаёт свой счёт.
KORT_COLUMN = "Q"
KORT_TITLE = "Код KORT"

#: Колонки загружаемого шаблона - только точные названия: из него пишут в книгу.
COLUMNS: tuple[Column, ...] = (
    Column("own", "Наша Фирма", ("наша фирма",), strict=True),
    Column("customer", "Заказчик (Название Фирмы)", ("заказчик (название фирмы)", "заказчик"), strict=True),
    Column("number", "№ Договора", ("№ договора",), strict=True),
    Column("period_from", "Период с (нач.)", ("период с (нач.)", "период с"), required=False, strict=True),
    Column("bill_no", "№ Счета", ("№ счета",), strict=True),
    Column("bill_date", "Дата выстав. Счета", ("дата выстав. счета", "дата выставления счета"), strict=True),
    Column("bill_period", "Счет за Период (Коммент)", ("счет за период (коммент)",), required=False, strict=True),
    Column("note", "Доп. Коммент.", ("доп. коммент.",), required=False, strict=True),
    Column("kort", KORT_TITLE, ("код kort",), required=False, strict=True),
)


class TemplateError(Exception):
    """Шаблон не разобрать - с текстом для человека."""


# ── Скачать ──────────────────────────────────────────────────────────────────


def _cell_value(bill: Bill, field: str) -> Any:
    if field in ("period_from", "period_to"):
        return getattr(bill, field)
    if field == "signed_at":
        return read_date(bill.signed_at) or (bill.signed_at or None)
    if field == "amount":
        return int(bill.amount) if bill.amount is not None and bill.amount == bill.amount.to_integral() else bill.amount
    if field == "bill_date":
        return read_date(bill.bill_date) or (bill.bill_date or None)
    if field == "bill_no":
        return bill.bill_no or None
    return getattr(bill, field) or None


def build(bill: Bill) -> bytes:
    """Шаблон одного счёта: известное из книги-сводки вписано, жёлтое - заполнить."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Счёт"
    bold = Font(bold=True)
    head = Alignment(wrap_text=True, vertical="center")
    for letter, title, width, field in LAYOUT:
        top = sheet[f"{letter}1"]
        top.value, top.font, top.alignment = title, bold, head
        sheet.column_dimensions[letter].width = width
        cell = sheet[f"{letter}2"]
        value = _cell_value(bill, field)
        if isinstance(value, Decimal):
            value = float(value)
        cell.value = value
        if field in ("signed_at", "period_from", "period_to", "bill_date"):
            cell.number_format = DATE_FORMAT
        if field == "amount":
            cell.number_format = "#,##0"
        if field in ("bill_no", "bill_date"):
            top.fill = FILL
            cell.fill = FILL
    for letter, width in GAPS.items():
        sheet.column_dimensions[letter].width = width
    sheet.row_dimensions[1].height = 32
    # Код счёта - в скрытой колонке: по нему KORT узнаёт свой шаблон, даже если
    # название клиента в файле поправили.
    sheet[f"{KORT_COLUMN}1"].value = KORT_TITLE
    sheet[f"{KORT_COLUMN}2"].value = bill.id
    sheet.column_dimensions[KORT_COLUMN].hidden = True
    rule = DataValidation(
        type="date",
        operator="greaterThan",
        formula1="DATE(2020,1,1)",
        showErrorMessage=True,
        errorTitle="Дата выставления",
        error="Впишите дату счёта: 22.10.2026",
    )
    rule.add("O2")
    sheet.add_data_validation(rule)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def filename(bill: Bill) -> str:
    """«Счёт ТОО KENESARY AGRO №ЮО-44 ОКТЯБРЬ 2026.xlsx» - без знаков, которые не любит файловая система."""
    name = " ".join(part for part in ("Счёт", bill.customer, bill.number, bill.month) if part)
    return re.sub(r'[\\/:*?"<>|]+', "-", name).strip()[:150] + ".xlsx"


# ── Загрузить ────────────────────────────────────────────────────────────────


def _open(data: bytes) -> Any:
    if not data:
        raise TemplateError("Файл пустой")
    if len(data) > MAX_BYTES:
        raise TemplateError("Файл больше 2 МБ - это не шаблон счёта")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_UNPACKED:
                raise TemplateError("Файл распаковывается в слишком большой объём - это не шаблон счёта")
    except zipfile.BadZipFile as exc:
        raise TemplateError("Это не файл .xlsx - сохраните шаблон в формате Excel (.xlsx)") from exc
    try:
        return load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl бросает много типов
        raise TemplateError("Файл не читается как .xlsx - сохраните шаблон в формате Excel (.xlsx)") from exc


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 36000 <= value <= 73000:
        return EPOCH + timedelta(days=int(value))  # дата числом в ячейке без формата даты
    text = str(value).strip()
    parsed = read_date(text)
    if parsed is not None:
        return parsed
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _find(sheet: Any) -> tuple[int, dict[str, int], list[tuple[Any, ...]]]:
    """Строка шапки, колонки по названиям и строки под шапкой."""
    rows = []
    for row in sheet.iter_rows(values_only=True, max_row=HEADER_SEARCH + MAX_ROWS):
        rows.append(tuple(row))
    for number, row in enumerate(rows[:HEADER_SEARCH]):
        header = [_text(cell) for cell in row]
        if not any(norm(cell) in ("№ счета",) for cell in header):
            continue
        try:
            layout = resolve_layout(sheet.title, COLUMNS, header)
        except LayoutError as exc:
            raise TemplateError(f"Шапку шаблона не разобрать: {exc}") from exc
        columns = {key: placement.index for key, placement in layout.placements.items() if placement.index is not None}
        return number, columns, rows[number + 1 :]
    raise TemplateError("В файле нет шапки шаблона - колонки «№ Счета» не нашлось")


def parse(data: bytes, bill: Bill) -> dict[str, Any]:
    """Поля карточки из заполненного шаблона: `values` (дата - в ISO) и где они нашлись.

    Пустые «Счет за Период» и «Доп. Коммент.» не возвращаются: пустая клетка
    файла не должна стирать то, что уже стоит в книге.
    """
    book = _open(data)
    try:
        found: list[tuple[str, int, dict[str, int], tuple[Any, ...]]] = []
        reasons: list[str] = []
        for sheet in book.worksheets:
            try:
                header_row, columns, rows = _find(sheet)
            except TemplateError as exc:
                reasons.append(f"«{sheet.title}»: {exc}")
                continue

            def cell(row: tuple[Any, ...], key: str, columns: dict[str, int] = columns) -> Any:
                index = columns.get(key)
                return row[index] if index is not None and index < len(row) else None

            indexed = list(enumerate(rows))
            by_code = [(i, row) for i, row in indexed if _text(cell(row, "kort")) == bill.id] if "kort" in columns else []
            if by_code:
                candidates = by_code
            else:
                candidates = [
                    (i, row)
                    for i, row in indexed
                    if norm(_text(cell(row, "own"))) == norm(bill.own)
                    and norm(_text(cell(row, "customer"))) == norm(bill.customer)
                    and norm(_text(cell(row, "number"))) == norm(bill.number)
                    and (
                        "period_from" not in columns
                        or bill.period_from is None
                        or _date(cell(row, "period_from")) in (None, bill.period_from)
                    )
                ]
            for i, row in candidates:
                # Строка листа: шапка - `header_row + 1`, данные - сразу под ней.
                found.append((sheet.title, header_row + 2 + i, columns, row))
        if not found:
            where = ", ".join(part for part in (bill.own, bill.customer, bill.number, bill.month) if part)
            extra = f" ({'; '.join(reasons)})" if reasons and len(reasons) == len(book.worksheets) else ""
            raise TemplateError(f"В шаблоне нет строки этого счёта: {where}{extra}")
        if len(found) > 1:
            rows_text = ", ".join(f"«{title}» строка {line}" for title, line, _, _ in found)
            raise TemplateError(f"Этот счёт стоит в шаблоне несколько раз ({rows_text}) - какую брать, KORT не угадывает")
        title, line, columns, row = found[0]

        def value(key: str) -> Any:
            index = columns.get(key)
            return row[index] if index is not None and index < len(row) else None

        if "kort" in columns and _text(value("kort")) == bill.id:
            same = (
                norm(_text(value("own"))) == norm(bill.own)
                and norm(_text(value("customer"))) == norm(bill.customer)
                and norm(_text(value("number"))) == norm(bill.number)
            )
            if not same:
                raise TemplateError(
                    f"В строке {line} шаблона другой договор, чем в карточке - шаблон правили. Скачайте его заново"
                )
        number = _text(value("bill_no"))
        if not number:
            raise TemplateError(f"В шаблоне (строка {line}) не вписан «№ Счета»")
        issued = _date(value("bill_date"))
        if issued is None:
            raw = _text(value("bill_date"))
            raise TemplateError(
                f"«Дата выстав. Счета» в строке {line} - " + (f"не дата: «{raw}»" if raw else "пусто")
            )
        values: dict[str, str] = {"bill_no": number, "bill_date": issued.isoformat()}
        for key in ("bill_period", "note"):
            text = _text(value(key))
            if text:
                values[key] = text
        return {"values": values, "sheet": title, "row": line}
    finally:
        book.close()


__all__ = ["TemplateError", "build", "filename", "parse"]
