"""Make relation extraction consume the persisted per-passage mention layer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from valuechain.edge_quality import normalize_dependency_object
from valuechain.entity_resolution import normalize_entity_key
from valuechain.models import EntityMention, Passage, RelationEvidence


GENERIC_PREFIXES = ("customer", "supplier", "vendor", "third-party", "limited", "single-source", "major ")


@dataclass
class MentionConstrainedExtractor:
    """Reject named relation objects that cannot be grounded in this passage.

    Anonymous dependency classes remain valid evidence/diagnostics, but cannot
    become canonical company edges.  A known alias is normalized back to the
    mention's proposed canonical name so LLM and rule extraction use one name.
    """

    extractor: object
    mentions_by_passage: dict[str, list[EntityMention]]
    diagnostics: list[dict[str, object]] = field(default_factory=list)

    def extract(self, passage: Passage) -> list[RelationEvidence]:
        return self._constrain(passage, self.extractor.extract(passage))

    async def extract_async(self, passage: Passage) -> list[RelationEvidence]:
        # The wrapper presents one interface to the pipeline, but rules mode is
        # deliberately synchronous. Do not make a saved-passage re-extraction
        # fail merely because the wrapper itself has an async method.
        if hasattr(self.extractor, "extract_async"):
            return self._constrain(passage, await self.extractor.extract_async(passage))
        return self.extract(passage)

    async def aclose(self) -> None:
        if hasattr(self.extractor, "aclose"):
            await self.extractor.aclose()

    def _constrain(self, passage: Passage, records: list[RelationEvidence]) -> list[RelationEvidence]:
        kept, diagnostics = constrain_relation_records(records, self.mentions_by_passage)
        self.diagnostics.extend(diagnostics)
        return kept


def matching_mention(value: str, mentions: list[EntityMention]) -> EntityMention | None:
    key = normalize_entity_key(_strip_footnote_marker(value))
    for mention in mentions:
        mention_keys = {
            normalize_entity_key(_strip_footnote_marker(mention.text)),
            normalize_entity_key(_strip_footnote_marker(mention.normalized_name)),
        }
        if key and key in mention_keys:
            return mention
    return None


def _strip_footnote_marker(value: str) -> str:
    """Remove parser-retained Exhibit 21 markers such as ``(1``."""
    return re.sub(r"\s*\(\d+\)?\s*$", "", value).strip()


def constrain_relation_records(
    records: list[RelationEvidence], mentions_by_passage: dict[str, list[EntityMention]]
) -> tuple[list[RelationEvidence], list[dict[str, object]]]:
    """Apply the same grounding policy to saved evidence during backfills."""
    kept: list[RelationEvidence] = []
    diagnostics: list[dict[str, object]] = []
    for record in records:
        mentions = [row for row in mentions_by_passage.get(record.passage_id, []) if row.mention_kind == "named_entity"]
        match = matching_mention(record.object, mentions)
        if match:
            # Exhibit 21 frequently adds a footnote marker to the legal name;
            # retain it in the source passage, never in the graph entity.
            kept.append(replace(record, object=_strip_footnote_marker(match.normalized_name)))
            continue
        object_info = normalize_dependency_object(record.object, subject=record.subject, evidence_text=record.evidence_text)
        if object_info.is_generic or object_info.object_kind == "geography" or not object_looks_named(record.object):
            kept.append(record)
            continue
        diagnostics.append({
            "passage_id": record.passage_id,
            "object": record.object,
            "relation_type": record.relation_type,
            "action": "drop_unmentioned_named_object",
            "reason": "No named entity mention in the same SEC passage supports this extracted counterparty.",
        })
    return kept, diagnostics


def object_looks_named(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.casefold()
    if not stripped or lowered.startswith(GENERIC_PREFIXES):
        return False
    if re.search(r"\b(inc|incorporated|corporation|corp|company|co|ltd|limited|plc|llc|holdings)\b", stripped, re.IGNORECASE):
        return True
    return bool(re.match(r"^[A-Z][A-Za-z0-9.&-]*(?:\s+[A-Z][A-Za-z0-9.&-]*){0,7}$", stripped))
