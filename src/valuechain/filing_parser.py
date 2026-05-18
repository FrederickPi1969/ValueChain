from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from valuechain.models import FilingRecord, Passage, Section


PARSER_NAME = "valuechain.sec_html_parser"
PARSER_VERSION = "0.1.0"


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


def normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_sections(filing: FilingRecord) -> list[Section]:
    path = Path(filing.local_path)
    warnings: list[str] = []
    text = html_file_to_text(path)
    if not text:
        return []
    patterns = SECTION_PATTERNS.get(filing.form, [])
    sections = split_sections(text, patterns)
    if not sections:
        warnings.append("target_sections_not_found_using_full_filing")
        sections = [("full_filing", text[:250_000])]
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
        match = found[1] if len(found) > 1 and found[0].start() < 10_000 else found[0]
        matches.append((section_name, match.start()))
    matches.sort(key=lambda item: item[1])
    sections: list[tuple[str, str]] = []
    for idx, (section_name, start) in enumerate(matches):
        end = matches[idx + 1][1] if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if len(body) >= 200:
            sections.append((section_name, body[:180_000]))
    return sections


def segment_passages(section: Section, max_chars: int = 1800) -> list[Passage]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", section.text) if part.strip()]
    passages: list[Passage] = []
    offset = 0
    for paragraph in paragraphs:
        clean = normalize_text(paragraph)
        if len(clean) < 80:
            continue
        for chunk in chunk_text(clean, max_chars=max_chars):
            passage_id = (
                f"{section.filing.ticker}_{section.filing.accession_no_dashes()}_"
                f"{section.section_name}_{offset}"
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
                    source_document_url=section.filing.primary_document_url,
                    section=section.section_name,
                    paragraph_offset=offset,
                    text=chunk,
                    parser_name=section.parser_name,
                    parser_version=section.parser_version,
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

