from valuechain.document_consistency import (
    AliasAutomaton,
    reconcile_document_mentions,
    reconcile_document_relations,
)
from valuechain.mention_layer import extract_passage_mentions
from valuechain.models import Passage, RelationEvidence


def passage(passage_id: str, text: str) -> Passage:
    return Passage(
        passage_id=passage_id,
        ticker="TSM",
        cik="1",
        company_name="Example Issuer",
        form="10-K",
        accession_number="a1",
        filing_date="2026-01-01",
        accepted_timestamp="",
        source_document_url="https://example.test/a1",
        section="business",
        paragraph_offset=0,
        text=text,
        parser_name="test",
        parser_version="1",
    )


def evidence(passage_id: str, relation_type: str, text: str) -> RelationEvidence:
    return RelationEvidence(
        subject="Example Issuer",
        object="Taiwan Semiconductor Manufacturing Company Limited",
        relation_type=relation_type,
        direction="subject_depends_on_object",
        modality="current_fact",
        certainty="high",
        temporal_scope="current",
        evidence_text=text,
        evidence_quote=text,
        confidence_score=0.8,
        extractor_model_version="test",
        ticker="EX",
        cik="1",
        form="10-K",
        filing_date="2026-01-01",
        accepted_timestamp="",
        accession_number="a1",
        source_document_url="https://example.test/a1",
        source_section="business",
        passage_id=passage_id,
        paragraph_offset=0,
        parser_name="test",
        parser_version="1",
    )


def test_aho_automaton_prefers_longest_word_bounded_alias() -> None:
    automaton = AliasAutomaton(
        {
            "Taiwan Semiconductor": "TSMC",
            "Taiwan Semiconductor Manufacturing Company Limited": "TSMC",
        }
    )
    matches = automaton.find("Taiwan Semiconductor Manufacturing Company Limited (TSMC)")
    assert matches[0][2] == "Taiwan Semiconductor Manufacturing Company Limited"
    assert not AliasAutomaton({"arm": "Arm Holdings"}).find("harmful")


def test_document_alias_is_rescanned_and_clustered_across_passages() -> None:
    passages = [
        passage("p1", "Taiwan Semiconductor Manufacturing Company Limited (TSMC) makes chips."),
        passage("p2", "TSMC supplies advanced wafers."),
    ]
    mentions = extract_passage_mentions(passages, [])
    reconciled, clusters, diagnostics = reconcile_document_mentions(passages, mentions)
    tsmc = [mention for mention in reconciled if mention.text == "TSMC"]
    assert len(tsmc) == 2
    assert len({mention.cluster_id for mention in tsmc}) == 1
    assert next(cluster for cluster in clusters if cluster.cluster_id == tsmc[0].cluster_id).mention_count >= 3
    assert any(row["action"] == "add_alias_mention" and row["passage_id"] == "p2" for row in diagnostics)


def test_document_relation_dedup_and_direction_conflict_are_explicit() -> None:
    text = "We rely on TSMC for wafer fabrication."
    supplier = evidence("p1", "supplier_dependency", text)
    duplicate = evidence("p2", "supplier_dependency", text)
    reverse = evidence("p3", "customer_dependency", "TSMC is our customer.")
    kept, diagnostics = reconcile_document_relations([supplier, duplicate, reverse])
    assert len(kept) == 2
    assert any(row["reason"] == "duplicate_across_passages" for row in diagnostics)
    assert all("document_direction_conflict" in row.risk_flags for row in kept)
    assert any(row["reason"] == "document_direction_conflict" for row in diagnostics)
