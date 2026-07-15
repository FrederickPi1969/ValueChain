from __future__ import annotations

import json
import re
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.multilingual.languages import (
    canonical_language,
    get_language_pack,
    infer_section,
    normalize_unicode,
)
from valuechain.financial_ie.multilingual.types import ParsedDocument, SourceDocument
from valuechain.financial_ie.retrieval import chunk_pages, extract_pdf_pages


PARSER_VERSION = "multilingual-document-parser-v0.1"
REPORT_FORMS = {
    "annual_report",
    "semiannual_report",
    "quarterly_report",
    "q1_report",
    "q3_report",
}
EDINET_FORM_MAP = {
    "120": "annual_report",
    "130": "annual_report",
    "140": "quarterly_report",
    "150": "quarterly_report",
    "160": "semiannual_report",
    "180": "material_event",
    "190": "material_event",
}


def load_source_document(path: Path) -> SourceDocument:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "filing" not in payload:
        return _load_taiwan_event(path, payload)
    filing = payload["filing"]
    source_id = str(filing.get("source_id") or "").strip()
    if source_id == "cninfo":
        return _load_cninfo(path, filing, payload)
    if source_id == "edinet":
        return _load_edinet(path, filing, payload)
    if source_id == "opendart":
        return _load_opendart(path, filing, payload)
    raise ValueError(f"Unsupported multilingual source: {source_id or path}")


def _load_cninfo(path: Path, filing: dict[str, Any], payload: dict[str, Any]) -> SourceDocument:
    metadata = dict(filing.get("metadata") or {})
    form = str(filing.get("form") or "announcement")
    document_path = _select_document_path(path, payload, preferred_suffixes=(".pdf",))
    return SourceDocument(
        source_id="cninfo",
        filing_id=str(filing.get("filing_id") or ""),
        issuer_id=str(filing.get("entity_id") or filing.get("source_entity_id") or ""),
        issuer_name=str(metadata.get("issuer_name") or metadata.get("secName") or ""),
        ticker=str(metadata.get("security_code") or metadata.get("secCode") or ""),
        language=canonical_language(str(filing.get("language") or "zh")),
        jurisdiction="CN",
        filing_type=form,
        filing_type_raw=str(filing.get("title") or form),
        title=str(filing.get("title") or ""),
        filed_at=str(filing.get("filed_at") or ""),
        source_url=str(filing.get("detail_url") or filing.get("primary_document_url") or ""),
        manifest_path=path,
        document_path=document_path,
        document_granularity="periodic_report" if form in REPORT_FORMS else "event_disclosure",
        metadata=metadata,
    )


def _load_edinet(path: Path, filing: dict[str, Any], payload: dict[str, Any]) -> SourceDocument:
    metadata = dict(filing.get("metadata") or {})
    raw_form = str(filing.get("form_raw") or metadata.get("docTypeCode") or "")
    form = EDINET_FORM_MAP.get(raw_form, "other_filing")
    document_path = _select_document_path(path, payload, preferred_suffixes=(".xbrl.zip", ".pdf"))
    return SourceDocument(
        source_id="edinet",
        filing_id=str(filing.get("source_filing_id") or metadata.get("docID") or ""),
        issuer_id=str(filing.get("source_issuer_id") or metadata.get("edinetCode") or ""),
        issuer_name=str(metadata.get("filerName") or ""),
        ticker=_edinet_ticker(str(metadata.get("secCode") or "")),
        language="ja",
        jurisdiction="JP",
        filing_type=form,
        filing_type_raw=raw_form,
        title=str(metadata.get("title") or metadata.get("docDescription") or ""),
        filed_at=str(filing.get("filing_date") or ""),
        source_url=str(filing.get("archive_url") or ""),
        manifest_path=path,
        document_path=document_path,
        document_granularity="periodic_report" if form in REPORT_FORMS else "event_disclosure",
        metadata=metadata,
    )


def _load_opendart(path: Path, filing: dict[str, Any], payload: dict[str, Any]) -> SourceDocument:
    metadata = dict(filing.get("metadata") or {})
    raw_form = str(filing.get("form_raw") or metadata.get("report_nm") or "")
    form = _classify_dart_form(raw_form)
    document_path = _select_document_path(path, payload, preferred_suffixes=(".zip", ".xml"))
    return SourceDocument(
        source_id="opendart",
        filing_id=str(filing.get("source_filing_id") or metadata.get("rcept_no") or ""),
        issuer_id=str(filing.get("source_issuer_id") or metadata.get("corp_code") or ""),
        issuer_name=str(metadata.get("corp_name") or metadata.get("flr_nm") or ""),
        ticker=str(metadata.get("stock_code") or ""),
        language="ko",
        jurisdiction="KR",
        filing_type=form,
        filing_type_raw=raw_form,
        title=str(metadata.get("title") or raw_form),
        filed_at=str(filing.get("filing_date") or metadata.get("rcept_dt") or ""),
        source_url=str(filing.get("archive_url") or ""),
        manifest_path=path,
        document_path=document_path,
        document_granularity="periodic_report" if form in REPORT_FORMS else "event_disclosure",
        metadata=metadata,
    )


def _load_taiwan_event(path: Path, payload: dict[str, Any]) -> SourceDocument:
    source_id = str(payload.get("source_id") or "")
    if source_id not in {"twse", "tpex"}:
        raise ValueError(f"Unsupported standalone JSON source: {source_id or path}")
    record = dict(payload.get("record") or {})
    ticker = str(record.get("公司代號") or "")
    return SourceDocument(
        source_id=source_id,
        filing_id=str(payload.get("filing_id") or path.stem),
        issuer_id=f"{source_id}-{ticker}",
        issuer_name=str(record.get("公司名稱") or ""),
        ticker=ticker,
        language="zh-Hant",
        jurisdiction="TW",
        filing_type="material_event",
        filing_type_raw="重大訊息",
        title=str(record.get("主旨 ") or record.get("主旨") or "重大訊息"),
        filed_at=str(payload.get("filed_at") or ""),
        source_url=str(payload.get("source_url") or ""),
        manifest_path=path,
        document_path=path,
        document_granularity="event_disclosure",
        metadata={"record": record, "snapshot_sha256": payload.get("snapshot_sha256")},
    )


def _select_document_path(
    manifest_path: Path,
    payload: dict[str, Any],
    *,
    preferred_suffixes: tuple[str, ...],
) -> Path:
    candidates = [
        Path(str(row.get("local_path")))
        for row in payload.get("documents", [])
        if isinstance(row, dict) and row.get("status") == "complete" and row.get("local_path")
    ]
    for suffix in preferred_suffixes:
        match = next((candidate for candidate in candidates if str(candidate).lower().endswith(suffix)), None)
        if match:
            return match
    if candidates:
        return candidates[0]
    siblings = [candidate for candidate in manifest_path.parent.iterdir() if candidate != manifest_path]
    for suffix in preferred_suffixes:
        match = next((candidate for candidate in siblings if str(candidate).lower().endswith(suffix)), None)
        if match:
            return match
    raise FileNotFoundError(f"No completed source document referenced by {manifest_path}")


def _classify_dart_form(value: str) -> str:
    compact = re.sub(r"\s+", "", value)
    if "사업보고서" in compact:
        return "annual_report"
    if "반기보고서" in compact:
        return "semiannual_report"
    if "분기보고서" in compact:
        return "quarterly_report"
    return "material_event"


def _edinet_ticker(value: str) -> str:
    return value[:-1] if len(value) == 5 and value.endswith("0") else value


def parse_source_document(source: SourceDocument) -> ParsedDocument:
    if not source.document_path.exists():
        raise FileNotFoundError(source.document_path)
    pack = get_language_pack(source.language)
    suffix = source.document_path.name.lower()
    warnings: list[str] = []
    if suffix.endswith(".pdf"):
        pages, parser_name, pdf_warnings = _extract_pdf_pages(source.document_path)
        warnings.extend(pdf_warnings)
        chunks = chunk_pages(pages, max_chars=2000, overlap_chars=180)
        chunks = [
            replace(chunk, section_hint=infer_section(chunk.text, pack) or chunk.section_hint)
            for chunk in chunks
        ]
    elif suffix.endswith(".zip"):
        if source.source_id == "edinet":
            chunks, warnings = _parse_edinet_zip(source.document_path, source.language)
            parser_name = "edinet_ixbrl_html"
        elif source.source_id == "opendart":
            chunks, warnings = _parse_dart_zip(source.document_path, source.language)
            parser_name = (
                "opendart_html_section_recovery"
                if "opendart_xml_recovered_with_html_parser" in warnings
                else "opendart_xml_sections"
            )
        else:
            raise ValueError(f"Unsupported ZIP source: {source.source_id}")
    elif suffix.endswith(".json"):
        payload = json.loads(source.document_path.read_text(encoding="utf-8"))
        text = normalize_unicode(str(payload.get("evidence_text") or ""))
        chunks = [DocumentChunk("event-c000", text, section_hint="material_event")] if text else []
        parser_name = "taiwan_material_event_json"
    else:
        raise ValueError(f"Unsupported document format: {source.document_path}")
    if not chunks:
        warnings.append("no_extractable_text")
    return ParsedDocument(
        source=source,
        chunks=chunks,
        parser_name=parser_name,
        parser_version=PARSER_VERSION,
        warnings=warnings,
        source_character_count=sum(len(chunk.text) for chunk in chunks),
    )


def _extract_pdf_pages(path: Path) -> tuple[list[str], str, list[str]]:
    try:
        return extract_pdf_pages(path), "pdftotext_layout", []
    except FileNotFoundError:
        from pypdf import PdfReader

        reader = PdfReader(path)
        pages = [normalize_unicode(page.extract_text() or "") for page in reader.pages]
        return pages, "pypdf_text", ["pdftotext_unavailable_used_pypdf"]


def _parse_edinet_zip(path: Path, language: str) -> tuple[list[DocumentChunk], list[str]]:
    chunks: list[DocumentChunk] = []
    warnings: list[str] = []
    pack = get_language_pack(language)
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if "XBRL/PublicDoc/" in name
            and name.lower().endswith((".htm", ".html", ".xhtml"))
            and "_ixbrl" in name.lower()
        )
        if not names:
            return [], ["edinet_public_ixbrl_missing"]
        for file_index, name in enumerate(names):
            raw = archive.read(name)
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup.find_all(["script", "style", "ix:hidden"]):
                tag.decompose()
            text = normalize_unicode(soup.get_text("\n"))
            if not text:
                continue
            section = infer_section(text, pack) or _first_heading(text)
            chunks.extend(
                _chunk_text(text, prefix=f"f{file_index:03d}", section_hint=section)
            )
    return _deduplicate_chunks(chunks), warnings


def _parse_dart_zip(path: Path, language: str) -> tuple[list[DocumentChunk], list[str]]:
    parser_warnings: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(name for name in archive.namelist() if name.lower().endswith(".xml"))
        if not names:
            return [], ["opendart_xml_missing"]
        raw = archive.read(max(names, key=lambda name: archive.getinfo(name).file_size))
    try:
        root = ET.fromstring(raw)
        chunks = _dart_section_chunks(root, language)
    except ET.ParseError:
        parser_warnings.append("opendart_xml_recovered_with_html_parser")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(raw, "html.parser")
        chunks = _dart_soup_section_chunks(soup, language)
    return _deduplicate_chunks(chunks), parser_warnings


def _dart_section_chunks(root: ET.Element, language: str) -> list[DocumentChunk]:
    pack = get_language_pack(language)
    elements = [element for element in root.iter() if _local_tag(element.tag) == "SECTION-2"]
    if not elements:
        elements = [element for element in root.iter() if _local_tag(element.tag) == "SECTION-1"]
    if not elements:
        elements = [root]
    chunks: list[DocumentChunk] = []
    for section_index, element in enumerate(elements):
        title = _element_title(element)
        text = normalize_unicode("\n".join(part for part in element.itertext() if part and part.strip()))
        if not text:
            continue
        section_hint = infer_section(f"{title}\n{text[:600]}", pack) or title[:120]
        chunks.extend(
            _chunk_text(text, prefix=f"s{section_index:03d}", section_hint=section_hint)
        )
    return chunks


def _dart_soup_section_chunks(soup: BeautifulSoup, language: str) -> list[DocumentChunk]:
    pack = get_language_pack(language)
    elements = soup.find_all("section-2") or soup.find_all("section-1")
    if not elements:
        elements = [soup]
    chunks: list[DocumentChunk] = []
    for section_index, element in enumerate(elements):
        title_node = element.find("title")
        title = normalize_unicode(title_node.get_text(" ") if title_node else "")
        text = normalize_unicode(element.get_text("\n"))
        if not text:
            continue
        section_hint = infer_section(f"{title}\n{text[:600]}", pack) or title[:120]
        chunks.extend(
            _chunk_text(text, prefix=f"r{section_index:03d}", section_hint=section_hint)
        )
    return chunks


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def _element_title(element: ET.Element) -> str:
    for child in element.iter():
        if _local_tag(child.tag) == "TITLE":
            return normalize_unicode(" ".join(child.itertext()))
    return ""


def _first_heading(text: str) -> str:
    for line in text.splitlines()[:30]:
        clean = " ".join(line.split()).strip()
        if 3 <= len(clean) <= 120:
            return clean
    return ""


def _chunk_text(
    text: str,
    *,
    prefix: str,
    section_hint: str,
    max_chars: int = 2000,
    overlap_chars: int = 180,
) -> list[DocumentChunk]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|(?<=[。！？.!?])\s*\n", text) if part.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [part.strip() for part in re.split(r"(?<=[。！？.!?])", text) if part.strip()]
    chunks: list[DocumentChunk] = []
    current = ""
    part_index = 0
    for paragraph in paragraphs:
        pieces = _hard_split(paragraph, max_chars)
        for piece in pieces:
            candidate = f"{current}\n{piece}".strip() if current else piece
            if current and len(candidate) > max_chars:
                chunks.append(
                    DocumentChunk(
                        chunk_id=f"{prefix}-c{part_index:03d}",
                        text=current,
                        section_hint=section_hint,
                    )
                )
                part_index += 1
                current = f"{current[-overlap_chars:]}\n{piece}".strip()
            else:
                current = candidate
    if current:
        chunks.append(
            DocumentChunk(
                chunk_id=f"{prefix}-c{part_index:03d}",
                text=current,
                section_hint=section_hint,
            )
        )
    return chunks


def _hard_split(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    boundaries = [match.end() for match in re.finditer(r"[。！？.!?；;]", text)]
    pieces: list[str] = []
    cursor = 0
    while len(text) - cursor > max_chars:
        candidates = [position for position in boundaries if cursor + max_chars // 2 <= position <= cursor + max_chars]
        end = candidates[-1] if candidates else cursor + max_chars
        pieces.append(text[cursor:end].strip())
        cursor = end
    if text[cursor:].strip():
        pieces.append(text[cursor:].strip())
    return pieces


def _deduplicate_chunks(chunks: Iterable[DocumentChunk]) -> list[DocumentChunk]:
    seen: set[str] = set()
    unique: list[DocumentChunk] = []
    for chunk in chunks:
        fingerprint = re.sub(r"\s+", "", chunk.text)
        if len(fingerprint) < 20 or fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(chunk)
    return unique
