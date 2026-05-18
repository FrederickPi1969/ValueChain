from pathlib import Path

from valuechain.dashboard import render_dashboard
from valuechain.models import GraphEdge, RelationEvidence


def test_render_dashboard_writes_html(tmp_path: Path) -> None:
    edge = GraphEdge(
        subject="A Corp",
        object="TSMC",
        relation_type="foundry_dependency",
        modality="current_fact",
        first_seen="2025-01-01",
        last_seen="2025-01-01",
        evidence_count=1,
        avg_confidence=0.8,
        forms="10-K",
        accessions="a1",
        source_urls="https://example.com",
    )
    record = RelationEvidence(
        subject="A Corp",
        object="TSMC",
        relation_type="foundry_dependency",
        direction="subject_depends_on_object",
        modality="current_fact",
        certainty="high",
        temporal_scope="as_disclosed",
        evidence_text="We rely on TSMC.",
        confidence_score=0.8,
        extractor_model_version="rules",
        ticker="A",
        cik="1",
        form="10-K",
        filing_date="2025-01-01",
        accepted_timestamp="",
        accession_number="a1",
        source_document_url="https://example.com",
        source_section="item_1_business",
        passage_id="p1",
        paragraph_offset=0,
        parser_name="parser",
        parser_version="0.1",
    )
    output = tmp_path / "dashboard.html"
    render_dashboard(output, [edge], [record], [{"symbol": "A", "sector": "Tech"}])
    html = output.read_text(encoding="utf-8")
    assert "AI Value Chain Disclosure Console" in html
    assert "dashboardData" in html
    assert "Company x Relation Heatmap" in html
    assert "TSMC" in html
