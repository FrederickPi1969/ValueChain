from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.multilingual.evidence import quote_in_text
from valuechain.financial_ie.multilingual.schema import (
    normalize_profile,
    normalize_signal_relation_payload,
    refresh_relation_review,
)


def test_unicode_quote_validation_tolerates_formatting_not_paraphrase() -> None:
    source = "主要客戶\n占本公司營業收入 ２２％。"
    assert quote_in_text("主要客戶占本公司營業收入22%", source)
    assert not quote_in_text("主要客戶占本公司營業收入25%", source)


def test_profile_normalization_uses_native_evidence() -> None:
    chunk = DocumentChunk("c1", "当社は半導体製造装置を開発しています。", section_hint="business")
    profile = normalize_profile(
        {
            "profile": {
                "business_summary_native": "当社は半導体製造装置を開発しています。",
                "business_summary_en": "The company develops semiconductor equipment.",
                "primary_industry": "semiconductors",
                "strategic_domains": ["semiconductor_supply_chain"],
                "value_chain_roles": ["equipment_provider"],
                "evidence": [
                    {"chunk_id": "c1", "quote_native": "半導体製造装置を開発しています"}
                ],
            }
        },
        {"c1": chunk},
    )
    assert profile["evidence_valid"] is True
    assert profile["translation_status"] == "model_generated_unverified"


def test_invalid_modality_is_not_defaulted_to_current_fact() -> None:
    chunk = DocumentChunk("c1", "若供应商中断供货，公司经营可能受到影响。")
    signals, _ = normalize_signal_relation_payload(
        {
            "signals": [
                {
                    "category": "supplier_or_infrastructure_dependency",
                    "headline_native": "供应风险",
                    "statement_native": "若供应商中断供货，公司可能受影响。",
                    "modality": "maybe_current",
                    "chunk_id": "c1",
                    "evidence_quote_native": "若供应商中断供货，公司经营可能受到影响。",
                }
            ]
        },
        {"c1": chunk},
    )
    assert signals[0]["modality"] is None
    assert signals[0]["review_status"] == "needs_review"


def test_relation_normalization_keeps_native_object_and_exact_quote() -> None:
    chunk = DocumentChunk("c1", "회사는 핵심 부품을 A사에서 단독 조달하고 있습니다.")
    _, relations = normalize_signal_relation_payload(
        {
            "relations": [
                {
                    "subject_native": "회사",
                    "object_native": "A사",
                    "relation_type": "supplier_dependency",
                    "direction": "subject_depends_on_object",
                    "modality": "current_fact",
                    "temporal_scope": "current",
                    "certainty": "explicit",
                    "confidence": 0.9,
                    "chunk_id": "c1",
                    "evidence_quote_native": "핵심 부품을 A사에서 단독 조달하고 있습니다",
                }
            ]
        },
        {"c1": chunk},
    )
    assert relations[0]["object_native"] == "A사"
    assert relations[0]["evidence_valid"] is True


def test_relation_semantic_guard_keeps_recall_but_blocks_bad_control_edge() -> None:
    chunk = DocumentChunk("c1", "Intel Corporation의 NAND 사업 영업양수를 완료했습니다.")
    _, relations = normalize_signal_relation_payload(
        {
            "relations": [
                {
                    "subject_native": "SK하이닉스",
                    "object_native": "Intel Corporation",
                    "relation_type": "subsidiary_or_control",
                    "direction": "subject_controls_object",
                    "modality": "current_fact",
                    "temporal_scope": "current",
                    "certainty": "explicit",
                    "chunk_id": "c1",
                    "evidence_quote_native": "Intel Corporation의 NAND 사업 영업양수를 완료했습니다.",
                }
            ]
        },
        {"c1": chunk},
    )
    assert len(relations) == 1
    assert relations[0]["evidence_valid"] is True
    assert relations[0]["semantic_warning"] == "control_not_explicit_in_quote"
    assert relations[0]["review_status"] == "needs_review"


def test_relation_semantic_guard_rejects_ordinal_as_entity_candidate() -> None:
    chunk = DocumentChunk("c1", "第一名客户销售额占比为22%。")
    _, relations = normalize_signal_relation_payload(
        {
            "relations": [
                {
                    "subject_native": "公司",
                    "object_native": "第一名",
                    "relation_type": "customer_dependency",
                    "direction": "subject_depends_on_object",
                    "modality": "historical_fact",
                    "temporal_scope": "historical",
                    "certainty": "explicit",
                    "chunk_id": "c1",
                    "evidence_quote_native": "第一名客户销售额占比为22%",
                }
            ]
        },
        {"c1": chunk},
    )
    assert relations[0]["semantic_warning"] == "non_entity_ordinal_object"
    assert relations[0]["review_status"] == "needs_review"


def test_control_direction_can_point_from_object_parent_to_subject_subsidiary() -> None:
    chunk = DocumentChunk("c1", "鴻海代子公司鴻揚創業投資股份有限公司公告投資事項。")
    _, relations = normalize_signal_relation_payload(
        {
            "relations": [
                {
                    "subject_native": "鴻揚創業投資股份有限公司",
                    "object_native": "鴻海",
                    "relation_type": "subsidiary_or_control",
                    "direction": "object_controls_subject",
                    "modality": "current_fact",
                    "temporal_scope": "current",
                    "certainty": "explicit",
                    "chunk_id": "c1",
                    "evidence_quote_native": "鴻海代子公司鴻揚創業投資股份有限公司公告投資事項",
                }
            ]
        },
        {"c1": chunk},
    )
    assert relations[0]["direction"] == "object_controls_subject"
    assert relations[0]["review_status"] == "candidate"


def test_dependency_direction_mismatch_is_not_graph_ready() -> None:
    chunk = DocumentChunk("c1", "防衛省向当社発注し、売上高の15%を占めています。")
    _, relations = normalize_signal_relation_payload(
        {
            "relations": [
                {
                    "subject_native": "当社",
                    "object_native": "防衛省",
                    "relation_type": "customer_dependency",
                    "direction": "object_depends_on_subject",
                    "modality": "current_fact",
                    "temporal_scope": "current",
                    "certainty": "explicit",
                    "chunk_id": "c1",
                    "evidence_quote_native": "防衛省向当社発注し、売上高の15%を占めています",
                }
            ]
        },
        {"c1": chunk},
    )
    assert relations[0]["semantic_warning"] == "direction_inconsistent_with_relation_type"
    assert relations[0]["review_status"] == "needs_review"


def test_anonymized_and_multi_entity_objects_are_not_graph_ready() -> None:
    base = {
        "relation_type": "supplier_dependency",
        "direction": "subject_depends_on_object",
        "modality": "current_fact",
        "temporal_scope": "current",
        "certainty": "explicit",
        "evidence_valid": True,
        "evidence_quote_native": "公司向供应商采购部件",
    }
    anonymous = {**base, "object_native": "VEN00737"}
    multiple = {**base, "object_native": "甲公司、乙公司"}
    refresh_relation_review(anonymous)
    refresh_relation_review(multiple)
    assert anonymous["semantic_warning"] == "anonymized_counterparty_object"
    assert multiple["semantic_warning"] == "multi_entity_object_requires_split"
    assert anonymous["review_status"] == multiple["review_status"] == "needs_review"
