from __future__ import annotations

import re
from dataclasses import dataclass

from valuechain.entity_resolution import EntityResolver
from valuechain.models import Passage, RelationEvidence


EXTRACTOR_VERSION = "rules-0.3.0-recall"


RELATION_PATTERNS: list[tuple[str, str, str, float]] = [
    ("foundry_dependency", r"\b(foundry|wafer fabrication|semiconductor fabrication)\b", "foundry capacity or service", 0.72),
    ("packaging_or_assembly_dependency", r"\b(advanced packaging|assembly|testing|outsourced semiconductor assembly|osat)\b", "packaging, assembly, or test provider", 0.70),
    ("manufacturing_dependency", r"\b(contract manufacturer|manufacturing partner|outsourced manufacturing|fabrication)\b", "manufacturing provider", 0.68),
    ("cloud_or_hosting_dependency", r"\b(cloud|hosting|aws|azure|google cloud|gcp)\b", "cloud or hosting provider", 0.68),
    ("data_center_dependency", r"\b(data centers?|colocation|compute capacity|server capacity|gpu capacity)\b", "data center or compute capacity", 0.68),
    (
        "power_or_utility_dependency",
        r"\b(power|electricity|utility|energy supply|cooling|natural gas|uranium|fuel|"
        r"power purchase agreements?|ppas?|transportation suppliers?)\b",
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
    ("supplier_dependency", r"\b(suppliers?|vendors?|third[- ]party|sole source|limited number)\b", "supplier(s)", 0.66),
    ("distribution_or_channel_dependency", r"\b(distributors?|resellers?|channel partners?|app store)\b", "distribution channel partner", 0.62),
    (
        "strategic_partner",
        r"\b(strategic partnership|strategic partner|strategic collaboration|collaboration agreement|"
        r"alliance|joint development)\b",
        "strategic partner",
        0.70,
    ),
    ("co_investment", r"\b(joint investment|co-investment|joint venture|jointly invest)\b", "co-investment partner", 0.70),
    ("licensing_dependency", r"\b(license|licensed technology|intellectual property|ip rights)\b", "licensor or licensed technology", 0.62),
    ("facility_or_geographic_exposure", r"\b(taiwan|china|export controls?|facility|geographic|earthquake|logistics)\b", "facility or geography", 0.58),
    ("concentration_risk", r"\b(concentration|limited number|single supplier|single customer|substantial portion)\b", "concentrated dependency", 0.68),
]


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
            if not re.search(pattern, lowered):
                continue
            modality = infer_modality(passage.section, lowered)
            certainty = "medium"
            confidence = base_confidence
            if modality == "current_fact":
                confidence += 0.08
                certainty = "high"
            elif modality == "risk_hypothetical":
                confidence -= 0.05
            object_mentions = self.resolver.resolve_objects(
                object_hint,
                text,
                subject_name=passage.company_name,
            )
            for object_mention in object_mentions:
                object_bonus = max(0.0, object_mention.confidence - 0.45) * 0.2
                records.append(
                    RelationEvidence(
                        subject=passage.company_name,
                        object=object_mention.normalized_name,
                        relation_type=relation_type,
                        direction="subject_depends_on_object",
                        modality=modality,
                        certainty=certainty,
                        temporal_scope=infer_temporal_scope(lowered),
                        evidence_text=text[:1800],
                        confidence_score=round(max(0.0, min(confidence + object_bonus, 0.95)), 3),
                        extractor_model_version=EXTRACTOR_VERSION,
                        ticker=passage.ticker,
                        cik=passage.cik,
                        form=passage.form,
                        filing_date=passage.filing_date,
                        accepted_timestamp=passage.accepted_timestamp,
                        accession_number=passage.accession_number,
                        source_document_url=passage.source_document_url,
                        source_section=passage.section,
                        passage_id=passage.passage_id,
                        paragraph_offset=passage.paragraph_offset,
                        parser_name=passage.parser_name,
                        parser_version=passage.parser_version,
                    )
                )
        return dedupe_records(records)


def infer_modality(section: str, lowered_text: str) -> str:
    if any(marker in lowered_text for marker in FORWARD_MARKERS):
        return "forward_looking"
    if "strategic partnership" in lowered_text or "strategic partner" in lowered_text:
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
