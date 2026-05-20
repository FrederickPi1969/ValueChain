from pathlib import Path

from valuechain.gleif import (
    EntityObjectContext,
    build_candidates_for_context,
    clean_gleif_query_object,
    confidence_band,
    context_from_group,
    is_likely_non_entity_object,
    normalize_legal_name,
    resolve_object_contexts,
    strip_legal_suffixes,
    write_candidate_queue,
)


def test_normalize_legal_name_strips_punctuation_and_suffix_core() -> None:
    assert normalize_legal_name("NVIDIA Corporation") == "nvidia corporation"
    assert strip_legal_suffixes(normalize_legal_name("NVIDIA Corporation")) == "nvidia"
    assert strip_legal_suffixes(normalize_legal_name("ASML Holding N.V.")) == "asml"


def test_class_objects_are_not_sent_to_gleif_by_default() -> None:
    assert is_likely_non_entity_object("single-source or limited-source suppliers")
    assert is_likely_non_entity_object("third-party data center providers")
    assert is_likely_non_entity_object("Foundry capacity class")
    assert is_likely_non_entity_object("Hong Kong")
    assert is_likely_non_entity_object("Pte Ltd")
    assert is_likely_non_entity_object("manufacturing partners")
    assert is_likely_non_entity_object("competitors")
    assert is_likely_non_entity_object("South Korea")
    assert is_likely_non_entity_object("Apple")
    assert is_likely_non_entity_object("Internet of Things")
    assert is_likely_non_entity_object("Light Company")
    assert not is_likely_non_entity_object("NVIDIA Corporation")
    assert not is_likely_non_entity_object("Extreme Networks")


def test_clean_gleif_query_object_removes_parser_prefixes() -> None:
    assert clean_gleif_query_object("Contents NVIDIA Corporation") == "NVIDIA Corporation"


def test_build_candidates_prefers_transliterated_name_match() -> None:
    context = EntityObjectContext(
        object="Taiwan Semiconductor Manufacturing Company Limited",
        evidence_count=4,
        subject_count=2,
        subjects="NVIDIA Corporation; Advanced Micro Devices Inc.",
        relation_types="foundry_dependency",
        modalities="current_fact",
        forms="10-K",
    )
    candidates = build_candidates_for_context(context, [(tsmc_record(), "fulltext")])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.lei == "549300KB6NK5SBD14S87"
    assert candidate.canonical_name == "Taiwan Semiconductor Manufacturing Company Limited"
    assert candidate.legal_name == "台灣積體電路製造股份有限公司"
    assert candidate.jurisdiction == "TW"
    assert candidate.confidence_band in {"high", "very_high"}


def test_resolve_object_contexts_keeps_no_match_rows_for_review() -> None:
    class FakeClient:
        def search_lei_records(self, query: str, page_size: int = 5):
            return []

    rows = resolve_object_contexts(
        [EntityObjectContext(object="Unmatched Counterparty LLC", evidence_count=1)],
        client=FakeClient(),  # type: ignore[arg-type]
    )
    assert rows[0].resolver_status == "no_gleif_match"
    assert rows[0].query_object == "Unmatched Counterparty LLC"


def test_resolve_object_contexts_skips_generic_classes_before_network() -> None:
    class FailingClient:
        def search_lei_records(self, query: str, page_size: int = 5):
            raise AssertionError("generic class objects should not be queried")

    rows = resolve_object_contexts(
        [EntityObjectContext(object="major customers", evidence_count=3)],
        client=FailingClient(),  # type: ignore[arg-type]
    )
    assert rows[0].resolver_status == "skipped_non_entity_object"


def test_context_from_group_preserves_review_context() -> None:
    context = context_from_group(
        "Microsoft Corporation",
        [
            {
                "subject": "OpenAI Supplier Inc.",
                "relation_type": "cloud_or_hosting_dependency",
                "modality": "current_fact",
                "form": "10-K",
                "evidence_text": "We rely on Microsoft Corporation for cloud services.",
            },
            {
                "subject": "Another Company",
                "relation_type": "strategic_partner",
                "modality": "strategic",
                "form": "8-K",
                "evidence_text": "We entered into a strategic collaboration agreement.",
            },
        ],
    )
    assert context.evidence_count == 2
    assert context.subject_count == 2
    assert "cloud_or_hosting_dependency" in context.relation_types
    assert "strategic" in context.modalities


def test_write_candidate_queue_writes_csv_jsonl_and_summary(tmp_path: Path) -> None:
    candidate = build_candidates_for_context(
        EntityObjectContext(object="NVIDIA Corporation", evidence_count=2),
        [(nvidia_record(), "exact_legal_name")],
    )[0]
    paths = write_candidate_queue(tmp_path, [candidate])
    assert Path(paths["csv"]).exists()
    assert Path(paths["jsonl"]).exists()
    assert Path(paths["summary"]).exists()
    assert "NVIDIA CORPORATION" in Path(paths["jsonl"]).read_text(encoding="utf-8")


def test_confidence_band_thresholds() -> None:
    assert confidence_band(0.96) == "very_high"
    assert confidence_band(0.9) == "high"
    assert confidence_band(0.75) == "medium"
    assert confidence_band(0.5) == "low"
    assert confidence_band(0.0) == "none"


def nvidia_record() -> dict:
    return {
        "id": "549300S4KLFTLO7GSQ80",
        "attributes": {
            "lei": "549300S4KLFTLO7GSQ80",
            "entity": {
                "legalName": {"name": "NVIDIA CORPORATION", "language": "en"},
                "otherNames": [],
                "transliteratedOtherNames": [],
                "legalAddress": {"country": "US", "region": "US-DE", "city": "WILMINGTON"},
                "headquartersAddress": {"country": "US", "region": "US-CA", "city": "SANTA CLARA"},
                "jurisdiction": "US-DE",
                "category": "GENERAL",
                "legalForm": {"id": "XTIQ"},
                "status": "ACTIVE",
                "registeredAt": {"id": "RA000602"},
                "registeredAs": "2862596",
            },
            "registration": {
                "status": "ISSUED",
                "corroborationLevel": "FULLY_CORROBORATED",
            },
            "bic": ["NVDAUS6SXXX"],
            "ocid": "us_de/2862596",
            "spglobal": ["32307"],
            "conformityFlag": "CONFORMING",
        },
        "relationships": {
            "direct-parent": {"links": {"reporting-exception": "https://api.gleif.org/direct-parent-exception"}},
            "ultimate-parent": {"links": {"reporting-exception": "https://api.gleif.org/ultimate-parent-exception"}},
        },
        "links": {"self": "https://api.gleif.org/api/v1/lei-records/549300S4KLFTLO7GSQ80"},
    }


def tsmc_record() -> dict:
    return {
        "id": "549300KB6NK5SBD14S87",
        "attributes": {
            "lei": "549300KB6NK5SBD14S87",
            "entity": {
                "legalName": {"name": "台灣積體電路製造股份有限公司", "language": "zh"},
                "otherNames": [],
                "transliteratedOtherNames": [
                    {
                        "name": "Taiwan Semiconductor Manufacturing Company Limited",
                        "language": "zh",
                        "type": "PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME",
                    }
                ],
                "legalAddress": {"country": "TW", "region": "", "city": "Hsinchu"},
                "headquartersAddress": {"country": "TW", "region": "", "city": "Hsinchu"},
                "jurisdiction": "TW",
                "category": "GENERAL",
                "legalForm": {"id": "TD8P"},
                "status": "ACTIVE",
                "registeredAt": {"id": "RA000551"},
                "registeredAs": "22099131",
            },
            "registration": {
                "status": "ISSUED",
                "corroborationLevel": "FULLY_CORROBORATED",
            },
            "qcc": "QTW1X4YKY8",
            "spglobal": ["380075"],
            "conformityFlag": "CONFORMING",
        },
        "relationships": {},
        "links": {"self": "https://api.gleif.org/api/v1/lei-records/549300KB6NK5SBD14S87"},
    }
    clean_gleif_query_object,
