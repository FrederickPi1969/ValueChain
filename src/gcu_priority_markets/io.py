from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import openpyxl
from bs4 import BeautifulSoup


_HEADER_CLEANER = re.compile(r"[\W_]+", flags=re.UNICODE)


def normalize_header(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return _HEADER_CLEANER.sub(" ", text).strip()


def decode_bytes(content: bytes, encodings: Iterable[str] | None = None) -> str:
    candidates = list(encodings or ("utf-8-sig", "utf-8", "gb18030", "cp949", "euc-kr", "cp1252", "latin-1"))
    for encoding in candidates:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _rows_from_csv(text: str) -> list[dict[str, Any]]:
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [{str(k or "").strip(): v for k, v in row.items()} for row in reader]


def _rows_from_html(text: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(text, "html.parser")
    best: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        headers = [cell.get_text(" ", strip=True) for cell in header_cells]
        if not headers:
            continue
        parsed: list[dict[str, Any]] = []
        for row in rows[1:]:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if not cells or not any(cells):
                continue
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            parsed.append(dict(zip(headers, cells, strict=False)))
        if len(parsed) > len(best):
            best = parsed
    return best


def _find_header_row(values: list[list[Any]]) -> int:
    for index, row in enumerate(values[:30]):
        normalized = {normalize_header(value) for value in row if value is not None}
        if len(normalized) >= 2 and any(
            token in normalized
            for token in {
                "symbol",
                "root ticker",
                "company name",
                "issuer name",
                "name",
                "security code",
                "종목코드",
                "회사명",
            }
        ):
            return index
    return 0


def _rows_from_xlsx(content: bytes) -> list[dict[str, Any]]:
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    output: list[dict[str, Any]] = []
    for worksheet in workbook.worksheets:
        values = [list(row) for row in worksheet.iter_rows(values_only=True)]
        if not values:
            continue
        header_index = _find_header_row(values)
        headers = [str(value or "").strip() for value in values[header_index]]
        for row in values[header_index + 1 :]:
            if not any(value not in (None, "") for value in row):
                continue
            record = dict(zip(headers, row, strict=False))
            record["__sheet__"] = worksheet.title
            output.append(record)
    return output


def _rows_from_xls(content: bytes) -> list[dict[str, Any]]:
    try:
        import xlrd
    except ImportError as exc:
        raise RuntimeError("Reading legacy .xls files requires the optional xlrd package") from exc
    workbook = xlrd.open_workbook(file_contents=content)
    output: list[dict[str, Any]] = []
    for sheet in workbook.sheets():
        values = [sheet.row_values(i) for i in range(sheet.nrows)]
        if not values:
            continue
        header_index = _find_header_row(values)
        headers = [str(value or "").strip() for value in values[header_index]]
        for row in values[header_index + 1 :]:
            if not any(value not in (None, "") for value in row):
                continue
            record = dict(zip(headers, row, strict=False))
            record["__sheet__"] = sheet.name
            output.append(record)
    return output


def read_tabular_bytes(
    content: bytes,
    filename: str = "payload.csv",
    encodings: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    suffix = Path(filename.split("?", 1)[0]).suffix.lower()
    stripped = content.lstrip()
    if suffix == ".json" or stripped.startswith((b"{", b"[")):
        payload = json.loads(decode_bytes(content))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("data", "rows", "items", "results", "records"):
                if isinstance(payload.get(key), list):
                    return [item for item in payload[key] if isinstance(item, dict)]
            return [payload]
    if suffix == ".xlsx" or content.startswith(b"PK"):
        try:
            return _rows_from_xlsx(content)
        except Exception:
            pass
    if suffix == ".xls" or content.startswith(b"\xd0\xcf\x11\xe0"):
        try:
            return _rows_from_xls(content)
        except Exception:
            pass
    text = decode_bytes(content, encodings)
    if "<table" in text.lower() or "<html" in text.lower():
        rows = _rows_from_html(text)
        if rows:
            return rows
    return _rows_from_csv(text)


def read_tabular_path(path: Path) -> list[dict[str, Any]]:
    return read_tabular_bytes(path.read_bytes(), path.name)


def alias_value(row: dict[str, Any], aliases: Iterable[str], default: Any = None) -> Any:
    normalized = {normalize_header(key): value for key, value in row.items()}
    for alias in aliases:
        value = normalized.get(normalize_header(alias))
        if value not in (None, ""):
            return value
    return default
