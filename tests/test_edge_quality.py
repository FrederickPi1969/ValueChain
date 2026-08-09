from valuechain.edge_quality import denoise_relation_evidence, evaluate_relation_evidence
from valuechain.models import RelationEvidence


def evidence(
    obj: str,
    relation_type: str,
    text: str,
    subject: str = "NVIDIA Corporation",
    modality: str = "current_fact",
) -> RelationEvidence:
    return RelationEvidence(
        subject=subject,
        object=obj,
        relation_type=relation_type,
        direction="subject_depends_on_object",
        modality=modality,
        certainty="high",
        temporal_scope="as_disclosed",
        evidence_text=text,
        confidence_score=0.76,
        extractor_model_version="rules",
        ticker="NVDA",
        cik="0001045810",
        form="10-K",
        filing_date="2026-02-25",
        accepted_timestamp="",
        accession_number="a1",
        source_document_url="https://example.com",
        source_section="item_1_business",
        passage_id="p1",
        paragraph_offset=0,
        parser_name="parser",
        parser_version="0.1",
    )


def test_denoise_keeps_specific_company_and_canonicalizes_alias() -> None:
    kept, diagnostics = denoise_relation_evidence(
        [
            evidence(
                "TSMC",
                "foundry_dependency",
                "We rely on TSMC for foundry and wafer fabrication capacity.",
            )
        ]
    )
    assert len(kept) == 1
    assert kept[0].object == "Taiwan Semiconductor Manufacturing Company Limited"
    assert diagnostics[0]["action"] == "keep"


def test_denoise_drops_generic_cloud_product_statement() -> None:
    kept, diagnostics = denoise_relation_evidence(
        [
            evidence(
                "cloud or hosting provider",
                "cloud_or_hosting_dependency",
                "We offer cloud services and AI tools to enterprise customers.",
                subject="Amazon.com Inc.",
            )
        ]
    )
    assert kept == []
    assert diagnostics[0]["reason"] in {"generic_object_not_graph_ready", "self_product_statement"}


def test_denoise_keeps_customer_concentration_class() -> None:
    kept, diagnostics = denoise_relation_evidence(
        [
            evidence(
                "major customer(s)",
                "customer_dependency",
                "One major customer accounted for a substantial portion of our revenue.",
            )
        ]
    )
    assert len(kept) == 1
    assert kept[0].object == "Major customer concentration class"
    assert diagnostics[0]["action"] == "keep"


def test_denoise_keeps_generic_supplier_for_recall_when_supported() -> None:
    kept, diagnostics = denoise_relation_evidence(
        [
            evidence(
                "single-source or limited-source suppliers",
                "supplier_dependency",
                "We rely on single-source or limited-source suppliers for key components.",
            )
        ]
    )
    assert len(kept) == 1
    assert kept[0].object == "single-source or limited-source suppliers"
    assert diagnostics[0]["action"] == "keep"


def test_denoise_keeps_generic_cloud_provider_when_third_party_supported() -> None:
    kept, diagnostics = denoise_relation_evidence(
        [
            evidence(
                "cloud computing platform providers",
                "cloud_or_hosting_dependency",
                "Interruptions or delays in services from third parties, including cloud computing platform providers, could harm us.",
                modality="risk_hypothetical",
            )
        ]
    )
    assert len(kept) == 1
    assert kept[0].object == "cloud computing platform providers"
    assert diagnostics[0]["action"] == "keep"


def test_quality_penalizes_self_product_without_dependency_signal() -> None:
    decision = evaluate_relation_evidence(
        evidence(
            "network or interconnection provider",
            "network_or_interconnection_dependency",
            "We provide networking products and compete in Ethernet switching markets.",
        )
    )
    assert decision.action == "drop"
    assert decision.quality_score < 0.4


def test_competition_context_is_retained_as_a_flagged_candidate() -> None:
    decision = evaluate_relation_evidence(
        evidence(
            "Microsoft Corporation",
            "strategic_partner",
            "If we lose Microsoft support for our products, our sales could be affected.",
        )
    )
    # This one remains a direct type mismatch and is still dropped.
    assert decision.action == "drop"
    assert decision.reason == "strategic_language_required"


def test_competitor_list_is_dropped_without_local_relationship_cue() -> None:
    decision = evaluate_relation_evidence(
        evidence(
            "Microsoft Corporation",
            "supplier_dependency",
            "We rely on a limited number of suppliers. Competition includes Microsoft Corporation and other cloud companies.",
        )
    )
    assert decision.action == "drop"
    assert decision.reason == "object_not_supported_for_relation"


def test_named_counterparty_requires_local_direct_cue() -> None:
    decision = evaluate_relation_evidence(
        evidence(
            "TSMC",
            "foundry_dependency",
            "We rely on Taiwan Semiconductor Manufacturing Company Limited for wafer fabrication capacity.",
        )
    )
    assert decision.action == "keep"


def test_strategic_deployment_list_does_not_become_cloud_dependency() -> None:
    decision = evaluate_relation_evidence(
        evidence(
            "Microsoft Corporation",
            "cloud_or_hosting_dependency",
            "AMD expanded collaboration with Microsoft to deploy AMD racks at scale on Azure.",
            modality="strategic",
        )
    )
    assert decision.action == "drop"
    assert decision.reason == "strategic_modality_requires_strategic_relation"


def test_trademark_reference_is_not_a_subsidiary_edge() -> None:
    decision = evaluate_relation_evidence(
        evidence(
            "Microsoft Corporation",
            "subsidiary_or_control",
            "Microsoft Windows is a registered trademark of Microsoft Corporation. Arm is a registered trademark of Arm Limited or its subsidiaries.",
        )
    )
    assert decision.action == "drop"
    assert decision.reason == "object_not_supported_for_relation"


def test_quality_drops_repeated_table_of_contents_header_as_counterparty() -> None:
    decision = evaluate_relation_evidence(
        evidence(
            "Contents NVIDIA Corporation",
            "concentration_risk",
            "Table of Contents NVIDIA Corporation and Subsidiaries. Revenue concentration is discussed below.",
        )
    )
    assert decision.action == "drop"
    assert decision.reason == "regulatory_or_fragment_object"
