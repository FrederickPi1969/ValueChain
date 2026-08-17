from pathlib import Path

from valuechain.dashboard import build_dashboard_data, render_dashboard
from valuechain.models import Company, FilingRecord, GraphEdge, Passage, RelationEvidence


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


def test_dashboard_includes_universe_companies_without_edges() -> None:
    companies = [
        Company(ticker="A", company_name="A Corp", role="accelerator_compute", priority=1),
        Company(ticker="B", company_name="B Corp", role="cloud_hyperscaler", priority=1),
    ]
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
    data = build_dashboard_data([edge], [], companies=companies)
    rows = {row["company"]: row for row in data["companies"]}

    assert data["summary"]["company_count"] == 2
    assert data["summary"]["active_company_count"] == 1
    assert set(rows) == {"A Corp", "B Corp"}
    assert rows["B Corp"]["edge_count"] == 0
    assert rows["B Corp"]["ticker"] == "B"


def test_dashboard_separates_network_ready_edges_from_generic_evidence() -> None:
    named = GraphEdge(
        subject="A Corp", object="TSMC", relation_type="foundry_dependency", modality="current_fact",
        first_seen="2025-01-01", last_seen="2025-01-01", evidence_count=1, avg_confidence=0.8,
        forms="10-K", accessions="a1", source_urls="https://example.com",
    )
    generic = GraphEdge(
        subject="A Corp", object="supplier(s)", relation_type="supplier_dependency", modality="current_fact",
        first_seen="2025-01-01", last_seen="2025-01-01", evidence_count=1, avg_confidence=0.8,
        forms="10-K", accessions="a1", source_urls="https://example.com",
    )
    data = build_dashboard_data([named, generic], [])
    assert data["summary"]["edge_count"] == 2
    assert data["summary"]["network_edge_count"] == 1
    assert data["network_edges"][0]["object"] == "TSMC"


def test_dashboard_prefers_canonical_supply_direction_for_network() -> None:
    edge = GraphEdge(
        subject="A Corp", object="TSMC", relation_type="foundry_dependency", modality="current_fact",
        first_seen="2025-01-01", last_seen="2025-01-01", evidence_count=1, avg_confidence=0.8,
        forms="10-K", accessions="a1", source_urls="https://example.com",
    )
    data = build_dashboard_data(
        [edge], [], canonical_relationships=[{
            "relationship_id": "rel-1", "supplier_name": "TSMC", "customer_name": "A Corp",
            "relationship_type": "supplies_to", "modality": "current_fact", "evidence_count": 2,
            "confidence": 0.9, "source_types": ["10-K"],
        }],
    )
    assert data["network_edges"] == [{
        "subject": "A Corp", "object": "TSMC", "relation_type": "supplies_to",
        "modality": "current_fact", "evidence_count": 2, "avg_confidence": 0.9,
        "first_seen": "", "last_seen": "", "forms": "10-K", "accessions": "", "source_urls": "", "relationship_id": "rel-1", "review_status": "unreviewed", "confirmation_status": "candidate", "relationship_family": "supply_chain", "categories": [], "product_or_service": "", "verification_status": "single_filing_candidate", "risk_flags": [], "source_role": "supplier", "target_role": "customer", "evidence_ids": [], "source_accession_numbers": [], "source_types": ["10-K"], "decision_reason": "", "decision_source": "", "llm_audit": {},
    }]


def test_dashboard_includes_canonical_audit_payload() -> None:
    entities = [{"entity_id": "entity:a", "canonical_name": "A Corp"}]
    relationships = [{"relationship_id": "rel-1", "supplier_name": "A Corp", "customer_name": "B Corp"}]
    diagnostics = [{"status": "unresolved_or_generic_counterparty", "raw_object": "supplier(s)"}]
    data = build_dashboard_data([], [], canonical_entities=entities, canonical_relationships=relationships, canonicalization_diagnostics=diagnostics)
    assert data["canonical_entities"] == entities
    assert data["canonical_relationships"] == relationships
    assert data["canonicalization_diagnostics"] == diagnostics
    assert data["summary"]["canonicalization_excluded_count"] == 1


def test_dashboard_company_context_includes_pipeline_coverage_counts() -> None:
    companies = [Company(ticker="NET", company_name="Cloudflare Inc.", role="edge_cloud_network", priority=2)]
    filings = [
        FilingRecord(
            ticker="NET",
            cik="1",
            company_name="Cloudflare Inc.",
            form="10-K",
            accession_number="a1",
            filing_date="2026-01-01",
        )
    ]
    passages = [
        Passage(
            passage_id="p1",
            ticker="NET",
            cik="1",
            company_name="Cloudflare Inc.",
            form="10-K",
            accession_number="a1",
            filing_date="2026-01-01",
            accepted_timestamp="",
            source_document_url="https://example.com",
            section="item_1_business",
            paragraph_offset=0,
            text="We rely on network providers.",
            parser_name="parser",
            parser_version="0.1",
        )
    ]
    data = build_dashboard_data(
        [],
        [],
        companies=companies,
        filings=filings,
        passages=passages,
        candidate_passages=passages,
    )
    row = data["companies"][0]
    assert row["filing_count"] == 1
    assert row["passage_count"] == 1
    assert row["candidate_passage_count"] == 1
    assert row["coverage_status"] == "candidate_only"
