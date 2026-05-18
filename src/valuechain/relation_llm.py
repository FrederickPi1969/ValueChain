from __future__ import annotations

from dataclasses import dataclass

from valuechain.llm_client import OpenAICompatibleClient
from valuechain.models import Passage, RelationEvidence


SYSTEM_PROMPT = """You extract financial-domain dependency relations from SEC filing passages.
Return only a JSON array. Do not include prose.
Each object must have: object, relation_type, modality, certainty, temporal_scope, confidence_score.
Use only these relation_type values:
supplier_dependency, customer_dependency, manufacturing_dependency, foundry_dependency,
packaging_or_assembly_dependency, cloud_or_hosting_dependency, data_center_dependency,
power_or_utility_dependency, network_or_interconnection_dependency,
distribution_or_channel_dependency, strategic_partner, co_investment,
licensing_dependency, facility_or_geographic_exposure, subsidiary_or_control,
concentration_risk.
Use modality values: current_fact, historical_fact, risk_hypothetical, forward_looking, strategic.
Do not convert hypothetical risk language into current_fact.
If no dependency relation is present, return [].
"""


@dataclass
class LLMRelationExtractor:
    client: OpenAICompatibleClient
    model_version: str

    def extract(self, passage: Passage) -> list[RelationEvidence]:
        return records_from_payload(
            passage,
            self.model_version,
            self.client.chat_json(SYSTEM_PROMPT, build_prompt(passage)),
        )

    async def extract_async(self, passage: Passage) -> list[RelationEvidence]:
        return records_from_payload(
            passage,
            self.model_version,
            await self.client.chat_json_async(SYSTEM_PROMPT, build_prompt(passage)),
        )

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
        obj = str(item.get("object", "")).strip()
        if not relation_type or not obj:
            continue
        records.append(
            RelationEvidence(
                subject=passage.company_name,
                object=obj,
                relation_type=relation_type,
                direction="subject_depends_on_object",
                modality=str(item.get("modality", "current_fact")),
                certainty=str(item.get("certainty", "medium")),
                temporal_scope=str(item.get("temporal_scope", "as_disclosed")),
                evidence_text=passage.text[:1800],
                confidence_score=float(item.get("confidence_score", 0.6)),
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
