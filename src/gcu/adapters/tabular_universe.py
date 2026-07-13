from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import openpyxl
import xlrd

from gcu.adapters.base import BaseAdapter
from gcu.models import EntityRef, SmokeResult, SmokeStatus


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def rows_from_excel(path: Path) -> Iterator[list[str]]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        # HKEX currently writes an incorrect worksheet dimension (A1:R8) even though the
        # XML contains thousands of rows. Resetting dimensions makes read-only iteration
        # inspect the actual stream instead of silently truncating the universe.
        sheet.reset_dimensions()
        for row in sheet.iter_rows(values_only=True):
            yield [_clean(value) for value in row]
        return
    if suffix == ".xls":
        workbook = xlrd.open_workbook(path)
        sheet = workbook.sheet_by_index(0)
        for index in range(sheet.nrows):
            yield [_clean(value) for value in sheet.row_values(index)]
        return
    raise ValueError(f"Unsupported workbook format: {path}")


def _header_index(
    rows: list[list[str]], aliases: dict[str, tuple[str, ...]]
) -> tuple[int, dict[str, int]]:
    normalized_aliases = {
        field: {alias.casefold().replace(" ", "").replace("_", "") for alias in names}
        for field, names in aliases.items()
    }
    for row_index, row in enumerate(rows[:50]):
        normalized = [cell.casefold().replace(" ", "").replace("_", "") for cell in row]
        mapping: dict[str, int] = {}
        for field, candidates in normalized_aliases.items():
            for column_index, cell in enumerate(normalized):
                if cell in candidates:
                    mapping[field] = column_index
                    break
        if "ticker" in mapping and "name" in mapping:
            return row_index, mapping
    raise ValueError("Could not locate ticker/name header row")


def parse_tabular_entities(
    rows: Iterable[list[str]],
    *,
    source_id: str,
    jurisdiction: str,
    exchange: str,
    aliases: dict[str, tuple[str, ...]],
) -> Iterable[EntityRef]:
    materialized = [row for row in rows if any(cell for cell in row)]
    header_row, mapping = _header_index(materialized, aliases)
    for row in materialized[header_row + 1 :]:

        def value(field: str, current_row: list[str] = row) -> str:
            index = mapping.get(field)
            return (
                current_row[index].strip() if index is not None and index < len(current_row) else ""
            )

        ticker = value("ticker")
        name = value("name")
        if not ticker or not name or ticker.casefold() in {"code", "ticker", "stockcode"}:
            continue
        isin = value("isin") or None
        market = value("market") or exchange
        entity_id = f"{source_id}-{ticker.replace(' ', '-').replace('/', '-')}"
        yield EntityRef(
            entity_id=entity_id,
            source_id=source_id,
            source_entity_id=ticker,
            legal_name=name,
            jurisdiction=jurisdiction,
            exchange=market,
            ticker=ticker,
            isin=isin,
            local_registry_id=value("registry") or None,
            metadata={
                "market": market,
                "industry": value("industry") or None,
                "category": value("category") or None,
                "raw_row": row,
            },
        )


class LocalUniverseAdapter(BaseAdapter):
    aliases: dict[str, tuple[str, ...]] = {
        "ticker": ("ticker", "code"),
        "name": ("company", "company name", "name"),
    }
    jurisdiction = ""
    exchange = ""

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        return SmokeResult(
            source_id=self.source_id,
            status=SmokeStatus.CONTRACT_VALIDATED,
            operation="tabular_universe_parser",
            endpoint=str(self.definition.official_url),
            message="Tabular universe parser contract validated; pass a downloaded official workbook/CSV to parse records.",
        )

    def parse_file(self, path: Path) -> Iterable[EntityRef]:
        if path.suffix.lower() in {".xls", ".xlsx", ".xlsm"}:
            rows = rows_from_excel(path)
        else:
            raw = path.read_bytes()
            text = None
            for encoding in ("utf-8-sig", "latin-1"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                raise ValueError(f"Could not decode {path}")
            try:
                dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;|\\t")
                delimiter = dialect.delimiter
            except csv.Error:
                # Metadata preambles can make Sniffer inconclusive. Choose the delimiter
                # that appears most often in the first plausible header line.
                candidates = [",", ";", "|", "\t"]
                lines = [line for line in text.splitlines() if line.strip()]
                delimiter = max(
                    candidates,
                    key=lambda item: max((line.count(item) for line in lines[:50]), default=0),
                )
            rows = (
                [cell.strip() for cell in row]
                for row in csv.reader(text.splitlines(), delimiter=delimiter)
            )
        for entity in parse_tabular_entities(
            rows,
            source_id=self.source_id,
            jurisdiction=self.jurisdiction,
            exchange=self.exchange,
            aliases=self.aliases,
        ):
            if self.accept_entity(entity):
                yield entity

    def accept_entity(self, entity: EntityRef) -> bool:
        return True

    def list_entities(self, *, path: Path, **_: Any) -> Iterable[EntityRef]:
        yield from self.parse_file(path)


class HkexUniverseAdapter(LocalUniverseAdapter):
    jurisdiction = "HK"
    exchange = "HKEX"
    aliases = {
        "ticker": ("Stock Code", "股份代號", "Code"),
        "name": ("Name of Securities", "Name of Issuer", "股份名稱", "Company Name"),
        "market": ("Market", "市場"),
        "isin": ("ISIN",),
        "category": ("Category",),
        "industry": ("Sub-Category",),
    }

    def accept_entity(self, entity: EntityRef) -> bool:
        return str(entity.metadata.get("category") or "").casefold() == "equity"


class JpxUniverseAdapter(LocalUniverseAdapter):
    jurisdiction = "JP"
    exchange = "JPX"
    aliases = {
        "ticker": ("Code", "Local Code", "Securities Code", "コード"),
        "name": ("Name", "Name (English)", "Company Name", "Issue Name", "銘柄名"),
        "market": ("Market/Products", "Section/Products", "Market", "市場・商品区分"),
        "industry": ("33 Sector(Code)", "33 Sector(name)", "Industry"),
    }

    def accept_entity(self, entity: EntityRef) -> bool:
        market = str(entity.metadata.get("market") or "")
        return "Market" in market


class AsxUniverseAdapter(LocalUniverseAdapter):
    jurisdiction = "AU"
    exchange = "ASX"
    aliases = {
        "ticker": ("ASX code", "Code", "Ticker"),
        "name": ("Company name", "Company", "Name"),
        "industry": ("GICS industry group", "Industry"),
    }


class NseIndiaUniverseAdapter(LocalUniverseAdapter):
    jurisdiction = "IN"
    exchange = "NSE"
    aliases = {
        "ticker": ("SYMBOL", "Symbol", "Ticker"),
        "name": ("NAME OF COMPANY", "Company Name", "Name"),
        "isin": ("ISIN NUMBER", "ISIN"),
        "industry": ("SERIES", "Industry"),
    }
