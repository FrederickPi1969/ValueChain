"""Safe re-extraction of relationships from an immutable saved SEC corpus.

This layer deliberately sits between acquisition and canonical publication. A
new extractor may improve text interpretation, but must first produce an
inspectable preview instead of silently replacing audited graph facts.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from valuechain.canonicalization import build_canonical_layer
from valuechain.config import Settings
from valuechain.edge_quality import denoise_relation_evidence
from valuechain.io_utils import read_jsonl, write_csv, write_json, write_jsonl
from valuechain.mention_constrained_extraction import MentionConstrainedExtractor
from valuechain.mention_layer import build_alias_review_queue, build_entity_triage, build_mention_clusters, extract_passage_mentions
from valuechain.models import EntityMention, Passage, RelationEvidence
from valuechain.pipeline import PipelineOptions, build_extractor, extract_relations
from valuechain.resolution_records import build_resolution_records
from valuechain.universe import read_universe


@dataclass(frozen=True)
class ReextractionPreview:
    preview_id: str
    preview_dir: Path
    summary: dict[str, Any]


def create_reextraction_preview(
    settings: Settings,
    run_id: str,
    *,
    extractor_name: str = "rules",
    llm_concurrency: int = 1,
    preview_id: str = "",
) -> ReextractionPreview:
    """Re-run extraction from saved passages and write isolated preview artifacts."""
    run_dir = settings.processed_dir / "runs" / run_id
    companies = read_universe(run_dir / "company_universe_resolved.csv")
    passages = [Passage(**row) for row in read_jsonl(run_dir / "passages.jsonl")]
    candidates = [Passage(**row) for row in read_jsonl(run_dir / "candidate_passages.jsonl")]
    if not companies or not passages or not candidates:
        raise ValueError("This run needs company_universe_resolved.csv, passages.jsonl, and candidate_passages.jsonl.")

    # Recompute the mention layer from the immutable passages so new mention
    # normalization (for example Exhibit 21 footnotes) is evaluated together
    # with the new extractor.
    mentions = extract_passage_mentions(passages, companies)
    clusters = build_mention_clusters(mentions)
    mentions_by_passage: dict[str, list[EntityMention]] = {}
    for mention in mentions:
        mentions_by_passage.setdefault(mention.passage_id, []).append(mention)
    options = PipelineOptions(
        universe_path=run_dir / "company_universe_resolved.csv",
        extractor=extractor_name,
        llm_concurrency=llm_concurrency,
    )
    base_extractor = build_extractor(settings, options, companies)
    extractor = MentionConstrainedExtractor(base_extractor, mentions_by_passage)
    raw = extract_relations(candidates, extractor, concurrency=llm_concurrency)
    evidence, denoise_diagnostics = denoise_relation_evidence(raw)
    entities, relationships, canonical_diagnostics = build_canonical_layer(companies, evidence)
    passages_by_id = {row.passage_id: row for row in passages}
    triage = build_entity_triage(mentions, clusters, passages_by_id)
    alias_queue = build_alias_review_queue(mentions, clusters, passages_by_id)
    resolution_records = build_resolution_records(mentions, clusters, passages, evidence)

    current_evidence = [RelationEvidence(**row).to_dict() for row in read_jsonl(run_dir / "relation_evidence.jsonl")]
    current_relationships = read_jsonl(run_dir / "canonical_relationships_reviewed.jsonl") or read_jsonl(run_dir / "canonical_relationships.jsonl")
    summary = compare_reextraction(current_evidence, [row.to_dict() for row in evidence], current_relationships, relationships)
    preview_id = preview_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    preview_dir = run_dir / "reextractions" / preview_id
    if preview_dir.exists():
        raise ValueError(f"Re-extraction preview already exists: {preview_id}")
    preview_dir.mkdir(parents=True)
    summary.update({"run_id": run_id, "preview_id": preview_id, "extractor": extractor_name, "llm_concurrency": llm_concurrency})
    write_jsonl(preview_dir / "relation_evidence_raw.jsonl", [row.to_dict() for row in raw])
    write_jsonl(preview_dir / "relation_evidence.jsonl", [row.to_dict() for row in evidence])
    write_csv(preview_dir / "mention_constraint_diagnostics.csv", extractor.diagnostics)
    write_csv(preview_dir / "merge_diagnostics.csv", denoise_diagnostics)
    write_jsonl(preview_dir / "entity_mentions.jsonl", [row.to_dict() for row in mentions])
    write_jsonl(preview_dir / "mention_clusters.jsonl", [row.to_dict() for row in clusters])
    write_csv(preview_dir / "entity_triage.csv", triage)
    write_csv(preview_dir / "alias_review_queue.csv", alias_queue)
    write_jsonl(preview_dir / "entity_resolution_records.jsonl", resolution_records)
    write_jsonl(preview_dir / "canonical_entities.jsonl", entities)
    write_jsonl(preview_dir / "canonical_relationships.jsonl", relationships)
    write_csv(preview_dir / "canonicalization_diagnostics.csv", canonical_diagnostics)
    write_json(preview_dir / "summary.json", summary)
    return ReextractionPreview(preview_id=preview_id, preview_dir=preview_dir, summary=summary)


def compare_reextraction(
    current_evidence: list[dict[str, Any]], preview_evidence: list[dict[str, Any]],
    current_relationships: list[dict[str, Any]], preview_relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize extraction change without assigning truth or overwriting review."""
    current_ids = {str(row.get("evidence_id", "")) for row in current_evidence}
    preview_ids = {str(row.get("evidence_id", "")) for row in preview_evidence}
    current_by_key = {relationship_key(row): row for row in current_relationships}
    preview_by_key = {relationship_key(row): row for row in preview_relationships}
    overlap = current_by_key.keys() & preview_by_key.keys()
    product_changed = sum(
        str(current_by_key[key].get("product_or_service", "")) != str(preview_by_key[key].get("product_or_service", ""))
        for key in overlap
    )
    prior_decision_ids = {str(row.get("relationship_id", "")) for row in current_relationships if row.get("decision") in {"accept", "reject", "review"}}
    preview_ids_by_relationship = {str(row.get("relationship_id", "")) for row in preview_relationships}
    return {
        "current_kept_assertions": len(current_evidence),
        "preview_kept_assertions": len(preview_evidence),
        "exact_assertion_id_overlap": len(current_ids & preview_ids),
        "preview_only_assertions": len(preview_ids - current_ids),
        "current_only_assertions": len(current_ids - preview_ids),
        "preview_assertions_with_product_or_service": sum(bool(row.get("product_or_service")) for row in preview_evidence),
        "current_relationships": len(current_relationships),
        "preview_relationships": len(preview_relationships),
        "semantic_relationship_overlap": len(overlap),
        "preview_only_semantic_relationships": len(preview_by_key.keys() - current_by_key.keys()),
        "current_only_semantic_relationships": len(current_by_key.keys() - preview_by_key.keys()),
        "shared_relationships_with_changed_product": product_changed,
        "preview_supply_chain_relationships": sum(row.get("relationship_family") == "supply_chain" for row in preview_relationships),
        "preview_relationship_types": dict(Counter(str(row.get("relationship_type", "")) for row in preview_relationships)),
        "prior_decision_ids_not_present_in_preview": len(prior_decision_ids - preview_ids_by_relationship),
    }


def relationship_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return tuple(str(row.get(field, "")) for field in (
        "source_entity_name", "target_entity_name", "relationship_type", "modality", "relationship_family",
    ))
