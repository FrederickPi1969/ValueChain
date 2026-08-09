"""Structured, append-only entity-resolution records before canonicalization."""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from typing import Any

from valuechain.edge_quality import object_key
from valuechain.models import EntityMention, MentionCluster, Passage, RelationEvidence
from valuechain.mention_layer import classify_cluster


def build_resolution_records(
    mentions: list[EntityMention], clusters: list[MentionCluster], passages: list[Passage], evidence: list[RelationEvidence]
) -> list[dict[str, Any]]:
    """Keep every relation-linked unresolved object; none is discarded here."""
    cluster_by_id = {row.cluster_id: row for row in clusters}
    mentions_by_key: dict[str, list[EntityMention]] = defaultdict(list)
    for mention in mentions:
        if mention.mention_kind == "named_entity":
            mentions_by_key[object_key(mention.normalized_name or mention.text)].append(mention)
    passage_by_id = {row.passage_id: row for row in passages}
    evidence_by_key: dict[str, list[RelationEvidence]] = defaultdict(list)
    for row in evidence:
        evidence_by_key[object_key(row.object)].append(row)
    records: list[dict[str, Any]] = []
    for key, evidence_rows in evidence_by_key.items():
        linked_mentions = mentions_by_key.get(key, [])
        cluster = next((cluster_by_id.get(row.cluster_id) for row in linked_mentions if row.cluster_id in cluster_by_id), None)
        display = (cluster.proposed_canonical_name if cluster else evidence_rows[0].object).strip()
        entity_class, disposition = classify_cluster(cluster) if cluster else ("untyped_relation_object", "retain_non_company")
        legal_candidate = bool(cluster and disposition == "review_organization_candidate")
        generic = not linked_mentions
        passage_ids = sorted({row.passage_id for row in evidence_rows})
        filing_ids = sorted({row.accession_number for row in evidence_rows if row.accession_number})
        issuers = sorted({row.subject for row in evidence_rows if row.subject})
        relationship_keys = {(row.subject, row.object, row.relation_type, row.modality) for row in evidence_rows}
        priority = round(len(evidence_rows) * 2 + len(filing_ids) * 3 + len(relationship_keys) * 2 + len(issuers), 2)
        record_id = f"resolution:{sha256((key + '|' + '|'.join(passage_ids)).encode()).hexdigest()[:20]}"
        records.append({
            "resolution_id": record_id,
            "cluster_id": cluster.cluster_id if cluster else "",
            "mention_text": display,
            "normalized_mention": key,
            "source_passage_ids": passage_ids,
            "source_filing_ids": filing_ids,
            "issuer_entity_ids": sorted({f"entity:{object_key(name)}" for name in issuers}),
            "issuer_names": issuers,
            "evidence_count": len(evidence_rows),
            "distinct_filing_count": len(filing_ids),
            "candidate_relationship_count": len(relationship_keys),
            "distinct_issuer_count": len(issuers),
            "graph_impact_score": len(relationship_keys),
            "priority_score": priority,
            "resolution_status": "candidate" if legal_candidate else "unresolved",
            "entity_class": entity_class,
            "status_reason": "Named organization requires candidate resolution." if legal_candidate else "Retained without legal-entity resolution; future evidence may change classification.",
            "sample_evidence": evidence_rows[0].evidence_text[:600],
            "sample_source_url": passage_by_id.get(passage_ids[0]).source_document_url if passage_ids and passage_ids[0] in passage_by_id else "",
            "candidate_entities": [],
            "resolution_evidence": [{"source": "sec_relation_evidence", "passage_ids": passage_ids, "filing_ids": filing_ids, "reason": "Original issuer disclosure containing the mention/object."}],
            "llm_assessments": [],
            "safety_validation": {},
            "decision": "KEEP_UNRESOLVED" if generic else "PENDING",
            "decision_reason": "Awaiting candidate generation." if legal_candidate else "No grounded legal-entity candidate yet.",
        })
    return sorted(records, key=lambda row: (-float(row["priority_score"]), str(row["mention_text"])))


def attach_resolution_candidates(records: list[dict[str, Any]], candidates_by_name: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    for row in records:
        row["candidate_entities"] = candidates_by_name.get(str(row["mention_text"]), [])
    return records


def attach_internal_resolution_candidates(
    records: list[dict[str, Any]], canonical_entities: list[dict[str, Any]], accepted_mappings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add closed-set graph/confirmed-alias candidates with their provenance.

    These are candidate-generation sources, not automatic canonicalization and
    not a substitute for an external registry such as GLEIF.
    """
    entity_by_key = {object_key(str(row.get("canonical_name", ""))): row for row in canonical_entities}
    aliases_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mapping in accepted_mappings:
        aliases_by_key[object_key(str(mapping.get("mention_text", "")))].append(mapping)
    for record in records:
        key = object_key(str(record.get("mention_text", "")))
        candidates = list(record.get("candidate_entities", []))
        evidence = list(record.get("resolution_evidence", []))
        entity = entity_by_key.get(key)
        if entity:
            candidates.append({"candidate_id": entity.get("entity_id"), "canonical_name": entity.get("canonical_name"), "source": "current_canonical_graph", "candidate_rank": 0})
            evidence.append({"source": "current_canonical_graph", "entity_id": entity.get("entity_id"), "reason": "Exact normalized name already exists in this run's canonical graph."})
        for mapping in aliases_by_key.get(key, []):
            candidates.append({"canonical_name": mapping.get("canonical_name"), "lei": mapping.get("lei", ""), "source": "confirmed_alias_history", "candidate_rank": 0})
            evidence.append({"source": "confirmed_alias_history", "reason": mapping.get("decision_reason", "Previously accepted alias mapping."), "policy_version": mapping.get("policy_version", "")})
        record["candidate_entities"] = candidates
        record["resolution_evidence"] = evidence
    return records
