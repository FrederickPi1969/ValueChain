from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from valuechain.models import FilingRecord, Passage, Section, SourceDocument


PARSER_NAME = "valuechain.sec_html_parser"
PARSER_VERSION = "0.3.0"
MAX_SECTION_START_RATIO = 0.99


SECTION_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "10-K": [
        ("item_1_business", r"\bitem\s+1[.\s:-]+business\b"),
        ("item_1a_risk_factors", r"\bitem\s+1a[.\s:-]+risk\s+factors\b"),
        ("item_7_mdna", r"\bitem\s+7[.\s:-]+management.?s\s+discussion\s+and\s+analysis\b"),
    ],
    "10-Q": [
        ("part_i_item_2_mdna", r"\bitem\s+2[.\s:-]+management.?s\s+discussion\s+and\s+analysis\b"),
        ("part_ii_item_1a_risk_factors", r"\bitem\s+1a[.\s:-]+risk\s+factors\b"),
    ],
    "8-K": [
        ("item_1_01_material_agreement", r"\bitem\s+1\.01[.\s:-]+entry\s+into\s+a\s+material\b"),
        ("item_2_02_results", r"\bitem\s+2\.02[.\s:-]+results\s+of\s+operations\b"),
        ("item_7_01_reg_fd", r"\bitem\s+7\.01[.\s:-]+regulation\s+fd\b"),
        ("item_8_01_other_events", r"\bitem\s+8\.01[.\s:-]+other\s+events\b"),
        ("item_9_01_exhibits", r"\bitem\s+9\.01[.\s:-]+financial\s+statements\s+and\s+exhibits\b"),
    ],
    "20-F": [
        ("item_3d_risk_factors", r"\bitem\s+3\.?d[.\s:-]+risk\s+factors\b"),
        ("item_4_company_information", r"\bitem\s+4[.\s:-]+information\s+on\s+the\s+company\b"),
        ("item_5_operating_review", r"\bitem\s+5[.\s:-]+operating\s+and\s+financial\s+review\b"),
    ],
}


def html_file_to_text(path: Path) -> str:
    raw = path.read_bytes()
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "ix:header", "header", "footer"]):
        tag.decompose()
    text = soup.get_text("\n")
    return normalize_text(text)


def html_table_rows_to_text(path: Path) -> str:
    raw = path.read_bytes()
    soup = BeautifulSoup(raw, "html.parser")
    rows: list[str] = []
    for tr in soup.find_all("tr"):
        cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            rows.append(" | ".join(cells))
    return "\n\n".join(row for row in rows if len(row) >= 20)


def normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_sections(filing: FilingRecord | SourceDocument) -> list[Section]:
    path = Path(filing.local_path)
    warnings: list[str] = []
    text = html_file_to_text(path)
    if not text:
        return []
    exhibit_section = exhibit_section_name(filing)
    if exhibit_section:
        if exhibit_section == "exhibit_21_subsidiaries":
            table_text = html_table_rows_to_text(path)
            if table_text:
                text = table_text
        warnings.append("source_document_parsed_as_full_exhibit")
        return [
            Section(
                filing=filing,
                section_name=exhibit_section,
                text=text[:250_000],
                parser_name=PARSER_NAME,
                parser_version=PARSER_VERSION,
                warnings=warnings.copy(),
            )
        ]
    patterns = SECTION_PATTERNS.get(filing.form, [])
    sections = split_sections(text, patterns)
    if not sections:
        warnings.append("target_sections_not_found_using_full_filing")
        sections = [(fallback_section_name(filing), text[:250_000])]
    return [
        Section(
            filing=filing,
            section_name=name,
            text=body,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            warnings=warnings.copy(),
        )
        for name, body in sections
    ]


def split_sections(text: str, patterns: list[tuple[str, str]]) -> list[tuple[str, str]]:
    lowered = text.lower()
    matches: list[tuple[str, int]] = []
    for section_name, pattern in patterns:
        found = list(re.finditer(pattern, lowered, flags=re.IGNORECASE))
        if not found:
            continue
        match = choose_section_match(found, len(text))
        if match is None:
            continue
        matches.append((section_name, match.start()))
    matches.sort(key=lambda item: item[1])
    sections: list[tuple[str, str]] = []
    for idx, (section_name, start) in enumerate(matches):
        end = matches[idx + 1][1] if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if len(body) >= 200:
            sections.append((section_name, body[:180_000]))
    return sections


def choose_section_match(matches: list[re.Match], text_length: int) -> re.Match | None:
    if not matches:
        return None
    latest_reasonable_start = int(text_length * MAX_SECTION_START_RATIO)
    viable = [match for match in matches if match.start() < latest_reasonable_start]
    if not viable:
        return None
    post_toc = [match for match in viable if match.start() > 10_000]
    return post_toc[0] if post_toc else viable[0]


def segment_passages(section: Section, max_chars: int = 1800) -> list[Passage]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", section.text) if part.strip()]
    passages: list[Passage] = []
    min_chars = 30 if section.section_name == "exhibit_21_subsidiaries" else 80
    offset = 0
    for paragraph in paragraphs:
        clean = normalize_text(paragraph)
        if len(clean) < min_chars:
            continue
        for chunk in chunk_text(clean, max_chars=max_chars):
            source_document = source_document_name(section.filing)
            source_document_type = source_document_type_name(section.filing)
            passage_id = (
                f"{section.filing.ticker}_{section.filing.accession_no_dashes()}_"
                f"{document_token(source_document)}_{section.section_name}_{offset}"
            )
            passages.append(
                Passage(
                    passage_id=passage_id,
                    ticker=section.filing.ticker,
                    cik=section.filing.cik,
                    company_name=section.filing.company_name,
                    form=section.filing.form,
                    accession_number=section.filing.accession_number,
                    filing_date=section.filing.filing_date,
                    accepted_timestamp=section.filing.accepted_timestamp,
                    source_document_url=source_document_url(section.filing),
                    section=section.section_name,
                    paragraph_offset=offset,
                    text=chunk,
                    parser_name=section.parser_name,
                    parser_version=section.parser_version,
                    source_document=source_document,
                    source_document_type=source_document_type,
                )
            )
            offset += 1
    return passages


def chunk_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current += " " + sentence
        else:
            chunks.append(current.strip())
            current = sentence
    if current:
        chunks.append(current.strip())
    return chunks


def source_document_url(filing: FilingRecord | SourceDocument) -> str:
    return getattr(filing, "document_url", "") or getattr(filing, "primary_document_url", "")


def source_document_name(filing: FilingRecord | SourceDocument) -> str:
    return getattr(filing, "document", "") or getattr(filing, "primary_document", "")


def source_document_type_name(filing: FilingRecord | SourceDocument) -> str:
    return getattr(filing, "document_type", "") or "PRIMARY"


def exhibit_section_name(filing: FilingRecord | SourceDocument) -> str:
    document_type = source_document_type_name(filing).upper()
    if document_type == "PRIMARY":
        return ""
    if document_type.startswith("EX-10"):
        return "exhibit_10_material_contract"
    if document_type.startswith("EX-21"):
        return "exhibit_21_subsidiaries"
    if document_type.startswith("EX-99.1"):
        return "exhibit_99_1_investor_or_earnings"
    if document_type.startswith("EX-99"):
        return "exhibit_99_investor_or_event_material"
    if document_type.startswith("EX-"):
        return "exhibit_other"
    return ""


def fallback_section_name(filing: FilingRecord | SourceDocument) -> str:
    if filing.form == "6-K":
        return "foreign_report_full_text"
    if filing.form == "8-K":
        return "event_report_full_text"
    return "full_filing"


def document_token(document: str) -> str:
    token = Path(document or "document").stem.lower()
    token = re.sub(r"[^a-z0-9]+", "_", token).strip("_")
    return token[:80] or "document"
