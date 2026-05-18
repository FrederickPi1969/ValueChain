from __future__ import annotations

from dataclasses import dataclass

from valuechain.llm_client import OpenAICompatibleClient
from valuechain.models import Passage, RelationEvidence


ALLOWED_RELATION_TYPES = {
    "supplier_dependency",
    "customer_dependency",
    "manufacturing_dependency",
    "foundry_dependency",
    "packaging_or_assembly_dependency",
    "cloud_or_hosting_dependency",
    "data_center_dependency",
    "power_or_utility_dependency",
    "network_or_interconnection_dependency",
    "distribution_or_channel_dependency",
    "strategic_partner",
    "co_investment",
    "licensing_dependency",
    "facility_or_geographic_exposure",
    "subsidiary_or_control",
    "concentration_risk",
}

ALLOWED_MODALITIES = {
    "current_fact",
    "historical_fact",
    "risk_hypothetical",
    "forward_looking",
    "strategic",
}

LOW_INFORMATION_OBJECTS = {
    "manufacturing_dependency",
    "concentration_risk",
    "supplier_dependency",
    "cloud_or_hosting_dependency",
}


SYSTEM_PROMPT = """You are a recall-first financial-domain relation extractor for SEC filing passages.
Return only a JSON array. Do not include prose, markdown, explanations, or trailing comments.

Task:
Extract evidence-backed dependency relations where the subject company depends on, is exposed to,
controls, partners with, or has concentrated exposure to a specific object disclosed in the passage.

Output schema for each JSON object:
- object: string. Prefer exact named counterparties when disclosed. If no name is disclosed, still output a
  useful class object such as "limited number of suppliers", "third-party data center providers",
  "channel partners", "natural gas transportation suppliers", "major customers", "fuel suppliers",
  or "cloud computing platform providers".
- object_kind: one of named_company, named_org, geography, facility, anonymous_counterparty,
  subsidiary_or_affiliate, dependency_class.
- relation_type: one of supplier_dependency, customer_dependency, manufacturing_dependency,
  foundry_dependency, packaging_or_assembly_dependency, cloud_or_hosting_dependency,
  data_center_dependency, power_or_utility_dependency, network_or_interconnection_dependency,
  distribution_or_channel_dependency, strategic_partner, co_investment, licensing_dependency,
  facility_or_geographic_exposure, subsidiary_or_control, concentration_risk.
- modality: one of current_fact, historical_fact, risk_hypothetical, forward_looking, strategic.
- certainty: high, medium, or low.
- temporal_scope: short string such as as_disclosed, FY2025, Q1 2026, multi-year, historical.
- evidence_quote: a short quote from the passage that directly supports the relation.
- confidence_score: number from 0 to 1.

Return at most 8 relation objects for one passage. Prefer named-counterparty evidence, but do not return []
just because the passage discloses only class-level dependencies or unnamed concentrated counterparties.
Keep evidence_quote to 25 words or fewer. Do not duplicate the same object/relation/modality.

Recall-first rules:
1. Emit a relation only when the passage directly supports the subject-object relation. Do not infer from
   co-occurrence, market category, or broad industry context.
2. For supplier, manufacturing, foundry, packaging, cloud, data center, power, network, distribution, or
   licensing dependencies, require explicit reliance language such as rely on, depend on, utilize, outsource,
   purchase from, obtain from, procure from, source from, hosted by, powered by, supplied by, constrained by,
   use third-party, contract with, or have contracts for.
3. For customer_dependency and concentration_risk, require concentration language, percentages, named large
   customers, "major customer", "limited number of customers", "large customers", or similar demand/revenue
   exposure. If customers are unnamed, output "Customer A", "Customer B", or "major customers".
4. For strategic_partner and co_investment, require explicit strategic partnership, alliance, joint development,
   collaboration agreement, joint venture, or co-investment language. Ordinary suppliers, customers,
   competitors, and ecosystem participants are not strategic partners.
5. For facility_or_geographic_exposure, emit only when the geography/facility is tied to operations, supply,
   manufacturing, data centers, revenue concentration, export controls, or disruption risk. Do not emit ordinary
   market names, segment names, headquarters locations, or sales regions without exposure.
6. Modality must follow the disclosure language:
   - current_fact: present operating dependency or relationship is directly stated.
   - historical_fact: past relationship or historical concentration.
   - risk_hypothetical: conditional risk-factor language, "may", "could", "if", possible disruption.
   - forward_looking: planned, expected, intended, future-oriented relation.
   - strategic: formal strategic partnership, co-investment, alliance, joint development, or collaboration.
7. If a passage says the subject sells cloud, AI, data center, power, semiconductor, networking, or software
   products, that is not a dependency by itself.
8. Class-level objects are allowed for recall when the passage discloses a real exposure but no named
   counterparty. Make the object descriptive, not a schema label: use "single-source suppliers", not
   "supplier_dependency"; use "third-party data center providers", not "data_center_dependency".
9. Do not output the subject company, its own products, its business segments, or its internal brands as objects.
10. When in doubt between [] and a directly supported class-level exposure, output the exposure with
    lower confidence rather than returning [].

Failure cases to avoid:
- "Power" as a company segment is not power_or_utility_dependency.
- A competitor list is not strategic_partner.
- AWS/Azure/GCP mentioned as a customer segment, product integration, or industry example is not cloud reliance.
- Do not use a relation_type value as the object.

Positive examples:
- "We rely on Taiwan Semiconductor Manufacturing Company Limited (TSMC) for wafers..." =>
  [{"object":"Taiwan Semiconductor Manufacturing Company Limited","object_kind":"named_company",
    "relation_type":"foundry_dependency","modality":"current_fact","certainty":"high",
    "temporal_scope":"as_disclosed","evidence_quote":"We rely on Taiwan Semiconductor Manufacturing Company Limited (TSMC) for... wafers",
    "confidence_score":0.95}]
- "Two customers accounted for 26% and 16% of revenue..." =>
  concentration_risk with objects "Customer A" and "Customer B", modality historical_fact or current_fact
  depending on the period language.
- "We rely on single-source or limited-source suppliers..." =>
  supplier_dependency with object "single-source or limited-source suppliers", modality current_fact or
  risk_hypothetical depending on the sentence.
- "Interruptions from third-party data center hosting facilities or cloud computing platform providers..." =>
  data_center_dependency with object "third-party data center hosting facilities" and
  cloud_or_hosting_dependency with object "cloud computing platform providers".
- "FPL had firm transportation contracts with ten different transportation suppliers..." =>
  power_or_utility_dependency with object "natural gas transportation suppliers".
- "The company held 32% of Digital Core REIT units..." =>
  subsidiary_or_control or facility_or_geographic_exposure only if the ownership/control relation is explicit.

If no dependency, exposure, concentration, control, or strategic relation is present, return [].
"""


@dataclass
class LLMRelationExtractor:
    client: OpenAICompatibleClient
    model_version: str

    def extract(self, passage: Passage) -> list[RelationEvidence]:
        try:
            payload = self.client.chat_json(SYSTEM_PROMPT, build_prompt(passage), max_tokens=1800)
        except Exception:
            return []
        return records_from_payload(passage, self.model_version, payload)

    async def extract_async(self, passage: Passage) -> list[RelationEvidence]:
        try:
            payload = await self.client.chat_json_async(SYSTEM_PROMPT, build_prompt(passage), max_tokens=1800)
        except Exception:
            return []
        return records_from_payload(passage, self.model_version, payload)

    async def aclose(self) -> None:
        await self.client.aclose()


def build_prompt(passage: Passage) -> str:
    return (
        f"Subject company: {passage.company_name}\n"
        f"Form: {passage.form}\n"
        f"Section: {passage.section}\n"
        f"Passage:\n{passage.text[:3500]}"
    )


def records_from_payload(
    passage: Passage,
    model_version: str,
    payload,
) -> list[RelationEvidence]:
    if not isinstance(payload, list):
        return []
    records: list[RelationEvidence] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        relation_type = str(item.get("relation_type", "")).strip()
        if relation_type not in ALLOWED_RELATION_TYPES:
            continue
        obj = normalize_object_payload(item.get("object", ""))
        if not obj or is_low_information_llm_object(obj, relation_type):
            continue
        modality = str(item.get("modality", "current_fact")).strip()
        if modality not in ALLOWED_MODALITIES:
            continue
        if relation_type in {"strategic_partner", "co_investment"} and modality != "strategic":
            continue
        confidence_score = normalize_confidence(item.get("confidence_score", 0.6))
        records.append(
            RelationEvidence(
                subject=passage.company_name,
                object=obj,
                relation_type=relation_type,
                direction="subject_depends_on_object",
                modality=modality,
                certainty=str(item.get("certainty", "medium")),
                temporal_scope=str(item.get("temporal_scope", "as_disclosed")),
                evidence_text=passage.text[:1800],
                confidence_score=confidence_score,
                extractor_model_version=model_version,
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
    return records


def normalize_object_payload(value) -> str:
    if isinstance(value, dict):
        for key in ["name", "text", "normalized_name", "entity", "label"]:
            candidate = str(value.get(key, "")).strip()
            if candidate:
                return candidate
        return ""
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def is_low_information_llm_object(obj: str, relation_type: str) -> bool:
    normalized = obj.strip().lower()
    relation_like = relation_type.lower().replace("_", " ")
    if normalized in LOW_INFORMATION_OBJECTS:
        return True
    if normalized == relation_type.lower() or normalized == relation_like:
        return True
    if normalized.endswith(" dependency") or normalized.endswith(" dependency class"):
        return True
    return False


def normalize_confidence(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.6
    return round(max(0.0, min(score, 1.0)), 3)


@dataclass
class HybridRelationExtractor:
    rules_extractor: object
    llm_extractor: LLMRelationExtractor | None = None

    def extract(self, passage: Passage) -> list[RelationEvidence]:
        rule_records = self.rules_extractor.extract(passage)
        if self.llm_extractor is None:
            return rule_records
        try:
            llm_records = self.llm_extractor.extract(passage)
        except Exception:
            return rule_records
        if not llm_records:
            return rule_records
        return merge_relation_records(rule_records, llm_records)

    async def extract_async(self, passage: Passage) -> list[RelationEvidence]:
        rule_records = self.rules_extractor.extract(passage)
        if self.llm_extractor is None:
            return rule_records
        try:
            llm_records = await self.llm_extractor.extract_async(passage)
        except Exception:
            return rule_records
        if not llm_records:
            return rule_records
        return merge_relation_records(rule_records, llm_records)

    async def aclose(self) -> None:
        if self.llm_extractor is not None:
            await self.llm_extractor.aclose()


def merge_relation_records(
    rule_records: list[RelationEvidence],
    llm_records: list[RelationEvidence],
) -> list[RelationEvidence]:
    merged = list(rule_records)
    existing = {
        (record.object.lower(), record.relation_type, record.modality)
        for record in rule_records
    }
    for record in llm_records:
        key = (record.object.lower(), record.relation_type, record.modality)
        if key not in existing:
            merged.append(record)
    return merged
