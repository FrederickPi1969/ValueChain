import json
import zipfile
from pathlib import Path

from valuechain.financial_ie.multilingual.documents import (
    _extract_pdf_pages,
    load_source_document,
    parse_source_document,
)


def test_parse_edinet_ixbrl_package(tmp_path: Path) -> None:
    package = tmp_path / "S100TEST.xbrl.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "XBRL/PublicDoc/0101010_honbun_test_ixbrl.htm",
            "<html><body><h1>事業の内容</h1><p>当社は半導体製造装置を開発し、世界の顧客に販売しています。</p></body></html>",
        )
    manifest = tmp_path / "filing.json"
    manifest.write_text(
        json.dumps(
            {
                "filing": {
                    "source_id": "edinet",
                    "source_filing_id": "S100TEST",
                    "source_issuer_id": "E00001",
                    "form_raw": "120",
                    "filing_date": "2025-06-01",
                    "archive_url": "https://example.test/S100TEST",
                    "metadata": {
                        "filerName": "テスト株式会社",
                        "secCode": "12340",
                        "title": "有価証券報告書",
                    },
                },
                "documents": [
                    {
                        "status": "complete",
                        "local_path": str(package),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = load_source_document(manifest)
    parsed = parse_source_document(source)
    assert source.ticker == "1234"
    assert source.filing_type == "annual_report"
    assert any("半導体製造装置" in chunk.text for chunk in parsed.chunks)
    assert parsed.chunks[0].section_hint == "business"


def test_parse_opendart_section_xml(tmp_path: Path) -> None:
    package = tmp_path / "20250001.zip"
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <DOCUMENT><BODY><SECTION-2><TITLE>II. 사업의 내용</TITLE>
    <P>회사는 반도체 장비를 생산하고 주요 고객에게 판매합니다.</P>
    </SECTION-2></BODY></DOCUMENT>"""
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("20250001.xml", xml)
    manifest = tmp_path / "filing.json"
    manifest.write_text(
        json.dumps(
            {
                "filing": {
                    "source_id": "opendart",
                    "source_filing_id": "20250001",
                    "source_issuer_id": "001",
                    "form_raw": "분기보고서 (2025.09)",
                    "filing_date": "2025-11-14",
                    "archive_url": "https://example.test/dart",
                    "metadata": {"corp_name": "테스트", "stock_code": "005930"},
                },
                "documents": [{"status": "complete", "local_path": str(package)}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    parsed = parse_source_document(load_source_document(manifest))
    assert parsed.source.filing_type == "quarterly_report"
    assert any("반도체 장비" in chunk.text for chunk in parsed.chunks)
    assert parsed.chunks[0].section_hint == "business"


def test_parse_taiwan_material_event_json(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "source_id": "twse",
                "filing_id": "twse-1",
                "filed_at": "2026-07-15",
                "source_url": "https://example.test/event",
                "evidence_text": "公司公告六月營收較去年同期成長20%。",
                "record": {"公司代號": "2330", "公司名稱": "台積電", "主旨 ": "六月營收"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    parsed = parse_source_document(load_source_document(path))
    assert parsed.source.language == "zh-Hant"
    assert parsed.source.document_granularity == "event_disclosure"
    assert parsed.chunks[0].text == "公司公告六月營收較去年同期成長20%。"


def test_pdf_parser_uses_pypdf_when_pdftotext_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    from pypdf import PdfWriter

    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)
    monkeypatch.setattr(
        "valuechain.financial_ie.multilingual.documents.extract_pdf_pages",
        lambda _: (_ for _ in ()).throw(FileNotFoundError()),
    )
    pages, parser_name, warnings = _extract_pdf_pages(path)
    assert pages == [""]
    assert parser_name == "pypdf_text"
    assert warnings == ["pdftotext_unavailable_used_pypdf"]
