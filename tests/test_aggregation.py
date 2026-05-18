from valuechain.aggregation import aggregate_edges, bottleneck_candidates
from valuechain.models import RelationEvidence


def evidence(subject: str, obj: str, relation_type: str, accession: str) -> RelationEvidence:
    return RelationEvidence(
        subject=subject,
        object=obj,
        relation_type=relation_type,
        direction="subject_depends_on_object",
        modality="current_fact",
        certainty="high",
        temporal_scope="as_disclosed",
        evidence_text="We rely on this provider.",
        confidence_score=0.8,
        extractor_model_version="rules",
        ticker="T",
        cik="1",
        form="10-K",
        filing_date="2025-01-01",
        accepted_timestamp="",
        accession_number=accession,
        source_document_url="https://example.com",
        source_section="item_1_business",
        passage_id=accession,
        paragraph_offset=0,
        parser_name="parser",
        parser_version="0.1",
    )


def test_aggregate_edges_groups_by_subject_object_relation_modality() -> None:
    edges = aggregate_edges(
        [
            evidence("A", "TSMC", "foundry_dependency", "a1"),
            evidence("A", "TSMC", "foundry_dependency", "a2"),
        ]
    )
    assert len(edges) == 1
    assert edges[0].evidence_count == 2
    assert edges[0].avg_confidence == 0.8


def test_bottleneck_candidates_detects_shared_object() -> None:
    edges = aggregate_edges(
        [
            evidence("A", "TSMC", "foundry_dependency", "a1"),
            evidence("B", "TSMC", "foundry_dependency", "b1"),
        ]
    )
    bottlenecks = bottleneck_candidates(edges)
    assert bottlenecks[0]["object"] == "TSMC"
    assert bottlenecks[0]["dependent_company_count"] == 2

