from __future__ import annotations

import re
from dataclasses import dataclass

from valuechain.entity_resolution import EntityResolver
from valuechain.models import Passage, RelationEvidence
from valuechain.ontology import raw_relation_types
from valuechain.ontology import orientation_for_raw


EXTRACTOR_VERSION = "rules-0.5.0-local-context-list-disaggregation"


RELATION_PATTERNS: list[tuple[str, str, str, float]] = [
    ("foundry_dependency", r"\b(foundr(?:y|ies)|wafer fabrication|semiconductor fabrication|semiconductor wafers?)\b", "foundry capacity or service", 0.72),
    ("packaging_or_assembly_dependency", r"\b(advanced packaging|assembly|testing|outsourced semiconductor assembly|osat)\b", "packaging, assembly, or test provider", 0.70),
    (
        "manufacturing_dependency",
        r"\b(contract manufacturer|manufacturing partner|outsourced manufacturing|fabrication|"
        r"manufacturing agreement|tolling agreement)\b",
        "manufacturing provider",
        0.68,
    ),
    (
        "cloud_or_hosting_dependency",
        r"\b(cloud|hosting|hosted by|cloud services agreement|aws|azure|google cloud|gcp)\b",
        "cloud or hosting provider",
        0.68,
    ),
    (
        "data_center_dependency",
        r"\b(data centers?|colocation|compute capacity|server capacity|gpu capacity|"
        r"data center lease|colocation agreement|capacity agreement)\b",
        "data center or compute capacity",
        0.68,
    ),
    (
        "power_or_utility_dependency",
        r"\b(power|electricity|utility|energy supply|cooling|natural gas|uranium|fuel|"
        r"power purchase agreements?|ppas?|energy services agreement|interconnection agreement|"
        r"transportation suppliers?)\b",
        "power, utility, or cooling supply",
        0.65,
    ),
    ("network_or_interconnection_dependency", r"\b(network|interconnection|peering|carrier|bandwidth|ethernet)\b", "network or interconnection provider", 0.62),
    (
        "customer_dependency",
        r"\b(customer concentration|major customers?|large customers?|significant customers?|"
        r"customers? accounted for|customer accounts?)\b",
        "major customer(s)",
        0.70,
    ),
    (
        "supplier_dependency",
        r"\b(suppliers?|vendors?|third[- ]party|sole source|limited number|supply agreement|"
        r"purchase agreement|master services agreement|services agreement|procurement agreement|"
        r"purchas(?:e|es|ed|ing)|procure(?:s|d|ment|ments)?|source(?:s|d|ing)?|obtain(?:s|ed|ing)?)\b",
        "supplier(s)",
        0.66,
    ),
    (
        "distribution_or_channel_dependency",
        r"\b(distributors?|resellers?|channel partners?|app store|distribution agreement|reseller agreement)\b",
        "distribution channel partner",
        0.62,
    ),
    (
        "strategic_partner",
        r"\b(strategic partnership|strategic partner|strategic collaboration|collaboration agreement|"
        r"alliance|joint development|lead partner|co-developer|strategic collaboration agreement)\b",
        "strategic partner",
        0.70,
    ),
    ("co_investment", r"\b(joint investment|co-investment|joint venture|jointly invest)\b", "co-investment partner", 0.70),
    ("asset_acquisition", r"\b(acquired assets? from|acquisition of assets? from|purchased assets? from|asset purchase agreement)\b", "asset seller", 0.76),
    ("asset_divestiture", r"\b(sold assets? to|divested assets? to|asset sale agreement|disposed of assets? to)\b", "asset buyer", 0.76),
    ("business_combination", r"\b(acquired|acquisition of|business combination|merger agreement|merged with)\b", "acquisition or merger counterparty", 0.74),
    ("strategic_investment", r"\b(strategic investment in|invested in|equity investment in|purchased .* equity)\b", "investment counterparty", 0.72),
    (
        "licensing_dependency",
        r"\b(license|license agreement|licensed technology|intellectual property|ip rights)\b",
        "licensor or licensed technology",
        0.62,
    ),
    ("facility_or_geographic_exposure", r"\b(taiwan|china|export controls?|facility|geographic|earthquake|logistics)\b", "facility or geography", 0.58),
    (
        "subsidiary_or_control",
        r"\b(subsidiar(?:y|ies)|wholly owned|majority owned|controlled by|parent company|"
        r"ownership interest|affiliates?)\b",
        "subsidiary or affiliate",
        0.66,
    ),
    ("concentration_risk", r"\b(concentration|limited number|single supplier|single customer|substantial portion)\b", "concentrated dependency", 0.68),
]

# The pattern list owns lexical triggers, while ontology owns the allowed
# vocabulary. Fail early rather than emitting a relation the next layer cannot
# interpret.
_unknown_pattern_types = {relation_type for relation_type, *_ in RELATION_PATTERNS} - raw_relation_types()
if _unknown_pattern_types:
    raise ValueError(f"Rule patterns contain types absent from ontology.yaml: {sorted(_unknown_pattern_types)}")


RISK_MARKERS = [
    "may adversely affect",
    "could adversely affect",
    "may be adversely affected",
    "could be adversely affected",
    "risk",
    "if we are unable",
    "if our suppliers",
    "if any supplier",
]

CURRENT_MARKERS = [
    "we rely",
    "we depend",
    "we are dependent",
    "we use",
    "we purchase",
    "we obtain",
    "we source",
    "we have entered into",
    "entered into",
    "contract with",
    "contracts with",
    "agreement with",
    "lease",
]

FORWARD_MARKERS = ["plan to", "expect to", "intend to", "will need", "future"]


@dataclass
class RuleBasedRelationExtractor:
    resolver: EntityResolver

    def extract(self, passage: Passage) -> list[RelationEvidence]:
        text = passage.text
        lowered = text.lower()
        records: list[RelationEvidence] = []
        for relation_type, pattern, object_hint, base_confidence in RELATION_PATTERNS:
            # Control relationships are read from the dedicated Exhibit 21
            # subsidiary list. Generic mentions of "subsidiaries" in financial
            # statements otherwise turn repeated page headers into fake entities.
            if relation_type == "subsidiary_or_control" and not passage.section.startswith("exhibit_21"):
                continue
            if passage.section.startswith("exhibit_21") and relation_type != "subsidiary_or_control":
                continue
            matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
            if not matches and not (
                passage.section.startswith("exhibit_21") and relation_type == "subsidiary_or_control"
            ):
                continue
            # A filing parser may combine several sentences into one Passage.
            # Bind each trigger to its local clause before resolving names so a
            # later customer/competitor list cannot inherit an earlier relation.
            if not matches:
                matches = [re.match("", text)]
            for match in matches:
                context = local_relation_context(text, match.start() if match else 0, match.end() if match else len(text))
                context_lowered = context.lower()
                modality = infer_modality(passage.section, context_lowered)
                certainty = "high" if modality == "current_fact" else "medium"
                confidence = base_confidence + (0.08 if modality == "current_fact" else -0.05 if modality == "risk_hypothetical" else 0)
                max_objects = 32 if relation_type == "subsidiary_or_control" or passage.section.startswith("exhibit_21") else 20
                object_mentions = self.resolver.resolve_objects(object_hint, context, subject_name=passage.company_name, max_objects=max_objects)
                for object_mention in object_mentions:
                    object_bonus = max(0.0, object_mention.confidence - 0.45) * 0.2
                    records.append(RelationEvidence(
                        subject=passage.company_name, object=object_mention.normalized_name, relation_type=relation_type,
                        direction="subject_depends_on_object", modality=modality, certainty=certainty,
                        temporal_scope=infer_temporal_scope(context_lowered), evidence_text=context[:1800],
                        confidence_score=round(max(0.0, min(confidence + object_bonus, 0.95)), 3),
                        extractor_model_version=EXTRACTOR_VERSION, ticker=passage.ticker, cik=passage.cik,
                        form=passage.form, filing_date=passage.filing_date, accepted_timestamp=passage.accepted_timestamp,
                        accession_number=passage.accession_number, source_document_url=passage.source_document_url,
                        source_section=passage.section, passage_id=passage.passage_id, paragraph_offset=passage.paragraph_offset,
                        parser_name=passage.parser_name, parser_version=passage.parser_version,
                        source_document=passage.source_document, source_document_type=passage.source_document_type,
                        evidence_quote=context[:700], direction_candidate=orientation_for_raw(relation_type),
                        trigger_text=match.group(0)[:160] if match else "", extractor_provenance=[EXTRACTOR_VERSION],
                        product_or_service=extract_product_or_service(context),
                    ))
        return dedupe_records(records)


def local_relation_context(text: str, start: int, end: int) -> str:
    """Return the sentence/bullet containing a trigger, capped to avoid bleed-over."""
    # A period inside "Co., Ltd." or "Inc.," is not a sentence boundary.
    boundaries = [match.end() for match in re.finditer(r"(?:\.(?=\s+[A-Z]|$)|;|\n|•)", text)]
    left = max((boundary for boundary in boundaries if boundary <= start), default=0)
    right = min((boundary for boundary in boundaries if boundary >= end), default=len(text))
    return text[left:right].strip()[:1800]


def infer_modality(section: str, lowered_text: str) -> str:
    if any(marker in lowered_text for marker in FORWARD_MARKERS):
        return "forward_looking"
    if any(marker in lowered_text for marker in ["strategic partnership", "strategic partner", "strategic collaboration", "lead partner", "co-developer"]):
        return "strategic"
    if "risk" in section or any(marker in lowered_text for marker in RISK_MARKERS):
        if any(marker in lowered_text for marker in CURRENT_MARKERS):
            return "current_fact"
        return "risk_hypothetical"
    if any(marker in lowered_text for marker in CURRENT_MARKERS):
        return "current_fact"
    if re.search(r"\b(previously|formerly|historically|during fiscal \d{4})\b", lowered_text):
        return "historical_fact"
    return "current_fact"


def infer_temporal_scope(lowered_text: str) -> str:
    match = re.search(r"\b(20\d{2}|fiscal\s+20\d{2}|quarter|annual|multi-year|long-term)\b", lowered_text)
    return match.group(0) if match else "as_disclosed"


def extract_product_or_service(context: str) -> str:
    """Conservative generic product span recovery; empty is preferable to guessing."""
    match = re.search(r"\b(?:purchase|purchases|purchased|procure|obtain|source)\s+(.{2,120}?)\s+\bfrom\b", context, re.IGNORECASE)
    if not match:
        match = re.search(r"\bfor\s+(assembly, testing and packaging|wafer fabrication|semiconductor wafers?|memory|[A-Za-z][A-Za-z -]{2,70}\bcapacity)\b", context, re.IGNORECASE)
    if not match:
        return ""
    value = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:")
    return value[:160]


def dedupe_records(records: list[RelationEvidence]) -> list[RelationEvidence]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[RelationEvidence] = []
    for record in records:
        key = (
            record.subject.lower(),
            record.object.lower(),
            record.relation_type,
            record.passage_id,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped
