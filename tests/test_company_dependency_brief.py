import csv
import json
from dataclasses import replace
from pathlib import Path

from valuechain.company_dependency_brief import (
    BriefClaim,
    BriefOptions,
    build_evidence_table,
    enforce_citation_constraints,
    evidence_id,
    filter_display_current_fact_evidence,
    filter_supported_current_fact_evidence,
    generate_company_dependency_brief,
    has_evidence_citation,
    invalid_citations,
    passage_quality_score,
    parse_lenient_json_content,
    select_top_operating_claims,
    select_strategic_evidence,
    uncited_interpretation_items,
    valid_citations,
    write_company_dependency_brief,
)


def test_generate_company_dependency_brief_with_llm_interpretation(tmp_path: Path) -> None:
    run_dir = make_brief_run(tmp_path)
    llm = FakeBriefLLM()
    brief = generate_company_dependency_brief(
        run_dir=run_dir,
        company_query="NVDA",
        llm_client=llm,
        model_version="Qwen/Qwen3.6-35B-A3B",
        options=BriefOptions(max_claims_per_section=4, max_evidence_table_rows=8),
    )
    assert brief.company["company_name"] == "NVIDIA Corporation"
    assert brief.company_role["brief_role_label"] == "accelerator_compute"
    assert brief.top_operating_dependencies[0].canonical_object == "Taiwan Semiconductor Manufacturing Company Limited"
    assert brief.top_operating_dependencies[0].object_lei == "549300KB6NK5SBD14S87"
    assert all("current_fact" in claim.modality_mix for claim in brief.top_operating_dependencies)
    assert all("risk_hypothetical" not in claim.modality_mix for claim in brief.top_operating_dependencies)
    assert "advanced packaging suppliers" not in {
        claim.canonical_object
        for claim in brief.top_operating_dependencies
    }
    assert any(claim.relation_type == "packaging_or_assembly_dependency" for claim in brief.top_risk_exposures)
    assert any(claim.relation_type == "strategic_partner" for claim in brief.strategic_relations)
    assert brief.analyst_interpretation["model_version"] == "Qwen/Qwen3.6-35B-A3B"
    assert brief.analyst_interpretation["generation_rounds"] == [
        "outline_planning",
        "final_writing",
        "citation_validation",
    ]
    assert brief.analyst_interpretation["valid_citations"]
    assert [call["stage"] for call in llm.calls] == ["outline", "final"]
    assert brief.evidence_table[0].source_document_url.startswith("https://www.sec.gov/")

    paths = write_company_dependency_brief(brief, tmp_path / "briefs")
    assert Path(paths["json"]).exists()
    markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "Company Dependency Brief: NVIDIA Corporation" in markdown
    assert "Taiwan Semiconductor Manufacturing Company Limited" in markdown
    assert "Writing outline" in markdown
    assert "Analyst interpretation" in markdown


def test_generate_company_dependency_brief_deterministic_fallback(tmp_path: Path) -> None:
    run_dir = make_brief_run(tmp_path)
    brief = generate_company_dependency_brief(
        run_dir=run_dir,
        company_query="NVIDIA Corporation",
        llm_client=None,
        model_version="deterministic",
    )
    assert brief.analyst_interpretation["model_version"] == "deterministic"
    assert brief.diagnostics["llm_enabled"] is False
    assert brief.diagnostics["company_evidence_rows"] == 3


def test_parse_lenient_json_content_handles_control_char_inside_string() -> None:
    parsed = parse_lenient_json_content('{"one_paragraph_summary":"line one\nline two","what_this_implies":[]}')
    assert parsed["one_paragraph_summary"] == "line one line two"


def test_invalid_citations_flags_claim_ids_and_unknown_evidence_ids() -> None:
    invalid = invalid_citations(
        {"one_paragraph_summary": "See E000000001FOU99 and S001, but H200 is a product."},
        {"E000000002FOU99"},
    )
    assert invalid == ["E000000001FOU99", "S001"]


def test_evidence_id_sanitizes_relation_codes_for_citation_regex() -> None:
    row = evidence_row(
        ticker="NVDA",
        cik="0001045810",
        subject="NVIDIA Corporation",
        obj="Partner LLC",
        relation_type="co_investment",
        modality="strategic",
        confidence=0.86,
        accession="0001045810-26-000031",
        paragraph=31,
        text="We made a co-investment with Partner LLC.",
    )
    eid = evidence_id(row)
    assert "_" not in eid
    assert eid == "E000031031COI86"
    assert has_evidence_citation(eid)
    assert valid_citations({"one_paragraph_summary": eid}, {eid}) == [eid]
    assert invalid_citations({"one_paragraph_summary": eid}, {eid}) == []


def test_evidence_table_preserves_first_duplicate_claim_mapping() -> None:
    row = evidence_row(
        ticker="NVDA",
        cik="0001045810",
        subject="NVIDIA Corporation",
        obj="Microsoft Corporation",
        relation_type="supplier_dependency",
        modality="current_fact",
        confidence=0.9,
        accession="0001045810-26-000050",
        paragraph=5,
        text="We rely on Microsoft Corporation.",
    )
    claim = BriefClaim(
        claim_id="C001",
        category="operating_dependency",
        relation_type="supplier_dependency",
        object="Microsoft Corporation",
        canonical_object="Microsoft Corporation",
        object_lei="",
        modality_mix="current_fact",
        evidence_count=1,
        avg_confidence=0.9,
        forms="10-K",
        accessions="0001045810-26-000050",
        first_seen="2026-02-20",
        last_seen="2026-02-20",
    )
    table = build_evidence_table([row], [claim, replace(claim, claim_id="F001")], max_rows=2, max_chars=200)
    assert table[0].claim_id == "C001"


def test_top_operating_excludes_heading_and_geography_fragments() -> None:
    rows = [
        evidence_row(
            ticker="AMZN",
            cik="0001018724",
            subject="Amazon.com Inc.",
            obj="Industry Risks",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.95,
            accession="0001018724-26-000001",
            paragraph=1,
            text="Item 1A. Risk Factors. Industry risks may affect us.",
        ),
        evidence_row(
            ticker="AMZN",
            cik="0001018724",
            subject="Amazon.com Inc.",
            obj="Australia",
            relation_type="customer_dependency",
            modality="current_fact",
            confidence=0.95,
            accession="0001018724-26-000001",
            paragraph=2,
            text="Australia's National Broadband Network is a customer.",
        ),
        evidence_row(
            ticker="AMZN",
            cik="0001018724",
            subject="Amazon.com Inc.",
            obj="AT&T",
            relation_type="customer_dependency",
            modality="current_fact",
            confidence=0.9,
            accession="0001018724-26-000001",
            paragraph=3,
            text="AT&T is a customer.",
        ),
    ]
    claims = select_top_operating_claims(rows, BriefOptions(max_claims_per_section=4))
    assert [claim.canonical_object for claim in claims] == ["AT&T"]


def test_low_quality_operating_patterns_are_demoted() -> None:
    rows = [
        evidence_row(
            ticker="ASML",
            cik="0000000001",
            subject="ASML Holding N.V.",
            obj="largest customer",
            relation_type="customer_dependency",
            modality="current_fact",
            confidence=0.98,
            accession="0000000001-26-000001",
            paragraph=1,
            text="The largest customer accounted for revenue.",
        ),
        evidence_row(
            ticker="ASML",
            cik="0000000001",
            subject="ASML Holding N.V.",
            obj="Carl Zeiss SMT",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.9,
            accession="0000000001-26-000001",
            paragraph=2,
            text="Carl Zeiss SMT supplies critical components.",
        ),
    ]
    claims = select_top_operating_claims(rows, BriefOptions(max_claims_per_section=4))
    assert [claim.canonical_object for claim in claims] == ["Carl Zeiss SMT"]


def test_top_operating_uses_descriptive_weak_objects_before_generic_classes() -> None:
    rows = [
        evidence_row(
            ticker="ASML",
            cik="0000000001",
            subject="ASML Holding N.V.",
            obj="Supplier dependency class",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.95,
            accession="0000000001-26-000001",
            paragraph=1,
            text="Supplier risk is disclosed.",
        ),
        evidence_row(
            ticker="ASML",
            cik="0000000001",
            subject="ASML Holding N.V.",
            obj="third-party vendors",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.9,
            accession="0000000001-26-000001",
            paragraph=2,
            text="We depend on third-party vendors for specialized components.",
        ),
    ]
    claims = select_top_operating_claims(rows, BriefOptions(max_claims_per_section=4))
    assert [claim.canonical_object for claim in claims] == ["third-party vendors"]


def test_strategic_relations_do_not_include_plain_licensing_dependencies() -> None:
    rows = [
        evidence_row(
            ticker="MSFT",
            cik="0000789019",
            subject="Microsoft Corporation",
            obj="License Counterparty LLC",
            relation_type="licensing_dependency",
            modality="current_fact",
            confidence=0.9,
            accession="0000789019-26-000001",
            paragraph=1,
            text="Microsoft has a license agreement with License Counterparty LLC.",
        ),
        evidence_row(
            ticker="MSFT",
            cik="0000789019",
            subject="Microsoft Corporation",
            obj="Strategic Partner LLC",
            relation_type="strategic_partner",
            modality="strategic",
            confidence=0.9,
            accession="0000789019-26-000001",
            paragraph=2,
            text="Microsoft announced a strategic partnership with Strategic Partner LLC.",
        ),
    ]
    selected = select_strategic_evidence(rows)
    assert [row["object"] for row in selected] == ["Strategic Partner LLC"]


def test_current_fact_display_filters_generic_classes_when_named_claims_exist() -> None:
    rows = [
        evidence_row(
            ticker="MSFT",
            cik="0000789019",
            subject="Microsoft Corporation",
            obj="Supplier dependency class",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.95,
            accession="0000789019-26-000001",
            paragraph=1,
            text="We rely on suppliers.",
        ),
        evidence_row(
            ticker="MSFT",
            cik="0000789019",
            subject="Microsoft Corporation",
            obj="Contoso Components LLC",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.9,
            accession="0000789019-26-000001",
            paragraph=2,
            text="We rely on Contoso Components LLC.",
        ),
    ]
    filtered = filter_display_current_fact_evidence(rows)
    assert [row["object"] for row in filtered] == ["Contoso Components LLC"]


def test_evidence_table_prefers_clean_passage_over_boilerplate() -> None:
    noisy = evidence_row(
        ticker="NVDA",
        cik="0001045810",
        subject="NVIDIA Corporation",
        obj="Taiwan Semiconductor Manufacturing Company Limited",
        relation_type="foundry_dependency",
        modality="current_fact",
        confidence=0.99,
        accession="0001045810-26-000010",
        paragraph=1,
        text="Item 1A Risk Factors Please carefully consider the following significant factors and uncertainties.",
    )
    clean = evidence_row(
        ticker="NVDA",
        cik="0001045810",
        subject="NVIDIA Corporation",
        obj="Taiwan Semiconductor Manufacturing Company Limited",
        relation_type="foundry_dependency",
        modality="current_fact",
        confidence=0.9,
        accession="0001045810-26-000010",
        paragraph=2,
        text="We rely on Taiwan Semiconductor Manufacturing Company Limited for wafer fabrication.",
    )
    claim = BriefClaim(
        claim_id="C001",
        category="operating_dependency",
        relation_type="foundry_dependency",
        object="Taiwan Semiconductor Manufacturing Company Limited",
        canonical_object="Taiwan Semiconductor Manufacturing Company Limited",
        object_lei="",
        modality_mix="current_fact",
        evidence_count=2,
        avg_confidence=0.945,
        forms="10-K",
        accessions="0001045810-26-000010",
        first_seen="2026-02-20",
        last_seen="2026-02-20",
    )
    assert passage_quality_score(clean) > passage_quality_score(noisy)
    table = build_evidence_table([noisy, clean], [claim], max_rows=2, max_chars=200)
    assert table[0].paragraph_offset == 2


def test_current_fact_support_filter_drops_competitor_landscape_relation() -> None:
    rows = [
        evidence_row(
            ticker="NVDA",
            cik="0001045810",
            subject="NVIDIA Corporation",
            obj="Microsoft Corporation",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.82,
            accession="0001045810-26-000021",
            paragraph=18,
            text=(
                "Competition includes large cloud services companies with internal teams designing hardware, "
                "such as Amazon and Microsoft Corporation."
            ),
        ),
        evidence_row(
            ticker="NVDA",
            cik="0001045810",
            subject="NVIDIA Corporation",
            obj="Taiwan Semiconductor Manufacturing Company Limited",
            relation_type="foundry_dependency",
            modality="current_fact",
            confidence=0.95,
            accession="0001045810-26-000021",
            paragraph=15,
            text="We utilize Taiwan Semiconductor Manufacturing Company Limited for wafer production.",
        ),
    ]
    filtered = filter_supported_current_fact_evidence(rows)
    assert [row["object"] for row in filtered] == ["Taiwan Semiconductor Manufacturing Company Limited"]


def test_current_fact_support_filter_drops_hypothetical_current_fact_relation() -> None:
    rows = [
        evidence_row(
            ticker="AMD",
            cik="0000002488",
            subject="Advanced Micro Devices Inc.",
            obj="Microsoft Corporation",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.82,
            accession="0000002488-26-000018",
            paragraph=1,
            text=(
                "If we lose Microsoft Corporation's support for our products, "
                "our ability to sell products could materially adversely affected."
            ),
        ),
        evidence_row(
            ticker="AMD",
            cik="0000002488",
            subject="Advanced Micro Devices Inc.",
            obj="Taiwan Semiconductor Manufacturing Company Limited",
            relation_type="supplier_dependency",
            modality="current_fact",
            confidence=0.9,
            accession="0000002488-26-000018",
            paragraph=17,
            text="We rely on Taiwan Semiconductor Manufacturing Company Limited for wafer fabrication.",
        ),
    ]
    filtered = filter_supported_current_fact_evidence(rows)
    assert [row["object"] for row in filtered] == ["Taiwan Semiconductor Manufacturing Company Limited"]


def test_strategic_filter_drops_negative_joint_venture_clause() -> None:
    rows = [
        evidence_row(
            ticker="AMD",
            cik="0000002488",
            subject="Advanced Micro Devices Inc.",
            obj="Broadcom Inc.",
            relation_type="co_investment",
            modality="current_fact",
            confidence=0.86,
            accession="0000002488-26-000018",
            paragraph=31,
            text=(
                "This Agreement shall not itself create or be deemed to create a joint venture, "
                "partnership or similar association between Broadcom Inc. and AMD."
            ),
        ),
        evidence_row(
            ticker="AMD",
            cik="0000002488",
            subject="Advanced Micro Devices Inc.",
            obj="OpenAI OpCo, LLC",
            relation_type="strategic_partner",
            modality="strategic",
            confidence=0.9,
            accession="0000002488-26-000018",
            paragraph=7,
            text="AMD entered into a strategic collaboration with OpenAI OpCo, LLC.",
        ),
    ]
    selected = select_strategic_evidence(rows)
    assert [row["object"] for row in selected] == ["OpenAI OpCo, LLC"]


def test_uncited_interpretation_items_flags_missing_citations() -> None:
    uncited = uncited_interpretation_items(
        {
            "one_paragraph_summary": "Summary has E000000001FOU99.",
            "what_this_implies": ["No citation here."],
            "what_to_monitor": ["Monitor this (E000000002SUP95)."],
            "weak_or_missing_evidence": ["Also no citation."],
        }
    )
    assert uncited == ["what_this_implies[0]", "weak_or_missing_evidence[0]"]


def test_enforce_citation_constraints_repairs_near_miss_and_uncited_items() -> None:
    cleaned = enforce_citation_constraints(
        {
            "one_paragraph_summary": "Bad near miss E000014011SUP79 and claim S001.",
            "what_this_implies": ["No citation here."],
            "what_to_monitor": ["Valid citation (E000004137SUP79)."],
            "weak_or_missing_evidence": ["Another bad claim C001."],
        },
        {"E000014011SUP74", "E000004137SUP79"},
    )
    serialized = json.dumps(cleaned)
    assert "E000014011SUP74" in serialized
    assert "E000014011SUP79" not in serialized
    assert "S001" not in serialized
    assert "C001" not in serialized
    assert uncited_interpretation_items(cleaned) == []
    assert cleaned["deterministic_citation_cleanup"] is True


def test_llm_report_writer_repairs_invalid_citations(tmp_path: Path) -> None:
    run_dir = make_brief_run(tmp_path)
    brief = generate_company_dependency_brief(
        run_dir=run_dir,
        company_query="NVDA",
        llm_client=FakeRepairLLM(),
        model_version="Qwen/Qwen3.6-35B-A3B",
        options=BriefOptions(max_claims_per_section=4, max_evidence_table_rows=8),
    )
    interpretation = brief.analyst_interpretation
    assert interpretation["citation_repair_attempted"] is True
    assert "citation_warnings" not in interpretation
    assert "S001" not in json.dumps(interpretation)


class FakeBriefLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat_json(self, system: str, user: str, max_tokens: int = 1200):
        assert "Taiwan Semiconductor" in user
        if "outline planner" in system:
            self.calls.append({"stage": "outline", "max_tokens": max_tokens})
            return {
                "dependency_thesis": [
                    {
                        "point": "NVIDIA has named foundry exposure to TSMC.",
                        "evidence_ids": ["E000010012FOU94"],
                        "strength": "high",
                    }
                ],
                "risk_focus": [
                    {
                        "point": "Packaging capacity appears as risk language.",
                        "evidence_ids": ["E000010020PAC76"],
                        "strength": "medium",
                    }
                ],
                "monitoring_plan": [
                    {
                        "point": "Monitor whether packaging risk becomes named current-fact capacity.",
                        "evidence_ids": ["E000010020PAC76"],
                        "strength": "medium",
                    }
                ],
                "evidence_limits": [
                    {
                        "point": "OpenAI relation is strategic but has one passage in this fixture.",
                        "evidence_ids": ["E000012004STR88"],
                        "strength": "low",
                    }
                ],
            }
        self.calls.append({"stage": "final", "max_tokens": max_tokens})
        assert "Seeking Alpha" in system
        return {
            "one_paragraph_summary": "NVIDIA has named foundry, packaging, and strategic partner evidence (E000010012FOU94).",
            "what_this_implies": [
                "Named current-fact dependencies are stronger than generic risk language (E000010012FOU94)."
            ],
            "what_to_monitor": [
                "Monitor whether packaging risk becomes named current-fact capacity (E000010020PAC76)."
            ],
            "weak_or_missing_evidence": [
                "The strategic OpenAI relation has one supporting passage in this fixture (E000012004STR88)."
            ],
        }


class FakeRepairLLM(FakeBriefLLM):
    def chat_json(self, system: str, user: str, max_tokens: int = 1200):
        if "repair" in system:
            return {
                "one_paragraph_summary": "NVIDIA has foundry evidence supported by TSMC disclosure (E000010012FOU94).",
                "what_this_implies": ["Foundry exposure is a current-fact dependency (E000010012FOU94)."],
                "what_to_monitor": ["Monitor packaging capacity language (E000010020PAC76)."],
                "weak_or_missing_evidence": ["Strategic evidence is thin in this fixture (E000012004STR88)."],
            }
        if "outline planner" in system:
            return super().chat_json(system, user, max_tokens=max_tokens)
        return {
            "one_paragraph_summary": "Bad draft cites a claim id and missing evidence (S001, E999999999BAD99).",
            "what_this_implies": ["Bad citation should be repaired (S001)."],
            "what_to_monitor": ["Monitor valid evidence too (E000010020PAC76)."],
            "weak_or_missing_evidence": ["Unsupported sentence (E999999999BAD99)."],
        }


def make_brief_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_csv(
        run_dir / "company_universe_resolved.csv",
        [
            {
                "ticker": "NVDA",
                "company_name": "NVIDIA Corporation",
                "role": "accelerator_compute",
                "priority": "1",
                "notes": "GPU accelerators networking systems software stack",
                "cik": "0001045810",
                "exchange": "Nasdaq",
            },
            {
                "ticker": "AMD",
                "company_name": "Advanced Micro Devices Inc.",
                "role": "accelerator_compute",
                "priority": "1",
                "notes": "",
                "cik": "0000002488",
                "exchange": "Nasdaq",
            },
        ],
    )
    write_csv(
        run_dir / "entity_resolution_llm_selected.csv",
        [
            {
                "query_object": "Taiwan Semiconductor Manufacturing Company Limited",
                "decision": "select",
                "selected_canonical_name": "Taiwan Semiconductor Manufacturing Company Limited",
                "selected_lei": "549300KB6NK5SBD14S87",
                "selected_jurisdiction": "TW",
            }
        ],
    )
    rows = [
        evidence_row(
            ticker="NVDA",
            cik="0001045810",
            subject="NVIDIA Corporation",
            obj="Taiwan Semiconductor Manufacturing Company Limited",
            relation_type="foundry_dependency",
            modality="current_fact",
            confidence=0.94,
            accession="0001045810-26-000010",
            paragraph=12,
            text="We depend on Taiwan Semiconductor Manufacturing Company Limited for foundry services.",
        ),
        evidence_row(
            ticker="NVDA",
            cik="0001045810",
            subject="NVIDIA Corporation",
            obj="advanced packaging suppliers",
            relation_type="packaging_or_assembly_dependency",
            modality="risk_hypothetical",
            confidence=0.76,
            accession="0001045810-26-000010",
            paragraph=20,
            text="We may be affected if advanced packaging suppliers cannot meet demand.",
        ),
        evidence_row(
            ticker="NVDA",
            cik="0001045810",
            subject="NVIDIA Corporation",
            obj="OpenAI OpCo, LLC",
            relation_type="strategic_partner",
            modality="strategic",
            confidence=0.88,
            accession="0001045810-26-000012",
            paragraph=4,
            text="We entered into a strategic collaboration with OpenAI OpCo, LLC.",
        ),
        evidence_row(
            ticker="AMD",
            cik="0000002488",
            subject="Advanced Micro Devices Inc.",
            obj="OpenAI OpCo, LLC",
            relation_type="strategic_partner",
            modality="strategic",
            confidence=0.9,
            accession="0000002488-26-000018",
            paragraph=7,
            text="AMD entered into a product purchase agreement with OpenAI OpCo, LLC.",
        ),
    ]
    with (run_dir / "relation_evidence.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return run_dir


def evidence_row(
    ticker: str,
    cik: str,
    subject: str,
    obj: str,
    relation_type: str,
    modality: str,
    confidence: float,
    accession: str,
    paragraph: int,
    text: str,
) -> dict:
    return {
        "ticker": ticker,
        "cik": cik,
        "subject": subject,
        "object": obj,
        "relation_type": relation_type,
        "direction": "subject_depends_on_object",
        "modality": modality,
        "certainty": "high",
        "temporal_scope": "current",
        "evidence_text": text,
        "confidence_score": confidence,
        "extractor_model_version": "test",
        "form": "10-K",
        "filing_date": "2026-02-20",
        "accepted_timestamp": "2026-02-20T12:00:00Z",
        "accession_number": accession,
        "source_document_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/doc.htm",
        "source_section": "Item 1A Risk Factors",
        "passage_id": f"{ticker}-{paragraph}",
        "paragraph_offset": paragraph,
        "parser_name": "test-parser",
        "parser_version": "0",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
