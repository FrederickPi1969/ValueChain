"""Strict, evidence-only LLM adjudication for canonical supply relationships."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from valuechain.dashboard import canonical_network_edges
from valuechain.edge_quality import object_key
from valuechain.evidence_identity import stable_evidence_id
from valuechain.llm_client import MAX_LLM_CONCURRENCY


SYSTEM_PROMPT = """You audit a proposed business relationship from an SEC filing.
Use ONLY the supplied evidence text. Do not use outside knowledge.

Return JSON with exactly: decision, confidence, reason, supported_relation_type, product_or_service, evidence_quote, direction_assessment.
decision must be one of accept, reject, review.
direction_assessment must be one of as_proposed, reverse, unclear. Use reverse only when the supplied evidence explicitly supports the same relation with the two named endpoints reversed.
Accept only when the text explicitly supports both named entities, the proposed direction, relationship family, and proposed relation type. Supply-chain, ownership/control, corporate transaction, and commercial relationships are distinct valid families; do not reject an ownership/control or corporate relationship merely because it is not a supply relationship.

Context injection rule: in SEC filings, every first-person issuer reference — "We", "Our", "Us", "the Company", "the Registrant" — refers to the reporting company identified as the customer or subject in proposed_relationship. Substitute that company before judging the relationship. Never reject merely because the excerpt uses one of these issuer references instead of its legal name.

A company merely named in a competitor list, market overview, customer list, or generic example is NOT by itself a business relationship: reject it unless the same supplied evidence explicitly states a relationship. Risk flags are warnings to inspect, not automatic decisions. A named subsidiary listed in Exhibit 21 can support an ownership/control relationship but not a supply relationship. Do not infer a relationship from a company's industry. If the excerpt is incomplete or ambiguous, use review. product_or_service must be a short phrase explicitly stated in evidence, or an empty string. evidence_quote must be a short exact quote from the supplied evidence, or an empty string."""


FOLLOW_UP_SYSTEM_PROMPT = """You are the second-pass explanation reviewer for an SEC relationship audit.
The first pass returned Reject or Review. Use only the supplied relationship, the explicitly mapped reporting-company context, and the SEC evidence. Return JSON with exactly: follow_up_reason, missing_or_conflicting_evidence, recommended_decision.
Explain concretely why the evidence does or does not support the relationship. Apply this binding rule: 'We', 'Our', 'Us', 'the Company', and 'the Registrant' refer to the stated reporting company. If that rule means the first pass appears mistaken, say so and set recommended_decision to accept, reject, or review. Do not invent facts."""


def audit_canonical_relationships(
    relationships: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    llm_client: Any,
    model_version: str,
    concurrency: int = 4,
) -> list[dict[str, Any]]:
    if not 1 <= concurrency <= MAX_LLM_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_LLM_CONCURRENCY}")
    return asyncio.run(audit_canonical_relationships_async(relationships, evidence, llm_client, model_version, concurrency))


async def audit_canonical_relationships_async(
    relationships: list[dict[str, Any]], evidence: list[dict[str, Any]], llm_client: Any,
    model_version: str, concurrency: int,
) -> list[dict[str, Any]]:
    evidence_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in evidence:
        # Retain the legacy passage index while new canonical relationships use
        # assertion-level IDs.  This keeps old runs auditable during migration.
        evidence_by_id.setdefault(stable_evidence_id(row), []).append(row)
        evidence_by_id.setdefault(str(row.get("passage_id", "")), []).append(row)
    semaphore = asyncio.Semaphore(concurrency)

    async def audit_one(relationship: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            if relationship.get("verification_status") == "cross_filing_verified":
                return automatic_cross_filing_audit(relationship)
            rows = evidence_for_relationship(relationship, evidence_by_id)
            payload = audit_payload(relationship, rows)
            try:
                response = await llm_client.chat_json_async(SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), max_tokens=420)
                audit = normalize_audit(relationship, response, model_version)
            except Exception as exc:
                audit = normalize_audit(relationship, {"decision": "review", "reason": f"LLM error: {exc}"}, model_version)
            if audit["decision"] in {"reject", "review"}:
                audit["initial_decision"] = audit["decision"]
                try:
                    follow_up = await llm_client.chat_json_async(
                        FOLLOW_UP_SYSTEM_PROMPT,
                        json.dumps({"proposed_relationship": payload["proposed_relationship"], "evidence": payload["evidence"], "first_pass": audit}, ensure_ascii=False),
                        max_tokens=360,
                    )
                    audit.update(normalize_follow_up(follow_up))
                    # The second pass is explicitly asked to challenge the
                    # first-pass conclusion using the issuer-pronoun context.
                    # When it reaches a valid conclusion, it becomes the
                    # current verdict while the original remains auditable.
                    if audit["recommended_decision"]:
                        audit["decision"] = audit["recommended_decision"]
                except Exception as exc:
                    audit["follow_up_reason"] = f"Second-pass LLM error: {exc}"[:900]
            return audit

    try:
        return await asyncio.gather(*(audit_one(row) for row in relationships))
    finally:
        if hasattr(llm_client, "aclose"):
            await llm_client.aclose()


def automatic_cross_filing_audit(relationship: dict[str, Any]) -> dict[str, Any]:
    """A deterministic acceptance is more auditable than asking an LLM to repeat it."""
    source_count = len(set(relationship.get("source_accession_numbers", [])))
    return {
        "relationship_id": relationship.get("relationship_id", ""),
        "decision": "accept",
        "confidence": 1.0,
        "reason": f"Cross-filing verification: {source_count} independent SEC filings support the same canonical fact.",
        "supported_relation_type": relationship.get("relationship_type", ""),
        "product_or_service": relationship.get("product_or_service", ""),
        "evidence_quote": "",
        "model_version": "cross_filing_rule",
        "decision_source": "cross_filing_rule",
    }


def evidence_for_relationship(
    relationship: dict[str, Any], evidence_by_id: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Keep only the evidence record that names this canonical counterparty."""
    rows = [item for evidence_id in relationship.get("evidence_ids", []) for item in evidence_by_id.get(str(evidence_id), [])]
    supplier_key = object_key(str(relationship.get("supplier_name", "")))
    direct = [row for row in rows if object_key(str(row.get("object", ""))) == supplier_key]
    if direct:
        return direct
    # Customer disclosures are oriented in the opposite direction in the raw
    # evidence, so use the customer as a secondary matching target.
    customer_key = object_key(str(relationship.get("customer_name", "")))
    direct = [row for row in rows if object_key(str(row.get("object", ""))) == customer_key]
    return direct or rows


def migrate_relationship_evidence_ids(
    relationships: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Upgrade legacy passage pointers in durable direction-correction records."""
    by_passage: dict[str, list[dict[str, Any]]] = {}
    available = {stable_evidence_id(row) for row in evidence}
    for item in evidence:
        by_passage.setdefault(str(item.get("passage_id", "")), []).append(item)
    migrated: list[dict[str, Any]] = []
    for relationship in relationships:
        row = dict(relationship)
        source_key = object_key(str(row.get("source_entity_name") or row.get("supplier_name") or ""))
        target_key = object_key(str(row.get("target_entity_name") or row.get("customer_name") or ""))
        upgraded: list[str] = []
        for legacy_id in row.get("evidence_ids", []):
            legacy_id = str(legacy_id)
            if legacy_id in available:
                upgraded.append(legacy_id)
                continue
            candidates = by_passage.get(legacy_id, [])
            exact = [
                item for item in candidates
                if {source_key, target_key}.issubset({object_key(str(item.get("subject", ""))), object_key(str(item.get("object", "")))})
            ]
            upgraded.extend(stable_evidence_id(item) for item in (exact or candidates))
        if upgraded:
            row["evidence_ids"] = sorted(set(upgraded))
            row["evidence_count"] = len(row["evidence_ids"])
        migrated.append(row)
    return migrated


def audit_payload(relationship: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "proposed_relationship": {
            "supplier": relationship.get("supplier_name", ""),
            "customer": relationship.get("customer_name", ""),
            "relationship_type": relationship.get("relationship_type", ""),
            "relationship_family": relationship.get("relationship_family", ""),
            "risk_flags": relationship.get("risk_flags", []),
        },
        "evidence": [
            {"form": row.get("form", ""), "section": row.get("source_section", ""), "quote": row.get("evidence_quote", ""), "direction_candidate": row.get("direction_candidate", "unclear"), "text": str(row.get("evidence_text", ""))[:6000]}
            for row in evidence[:6]
        ],
    }


def normalize_audit(relationship: dict[str, Any], raw: Any, model_version: str) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    decision = str(raw.get("decision", "review")).lower()
    if decision not in {"accept", "reject", "review"}:
        decision = "review"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "relationship_id": relationship.get("relationship_id", ""),
        "decision": decision,
        "confidence": round(confidence, 3),
        "reason": str(raw.get("reason", ""))[:900],
        "supported_relation_type": str(raw.get("supported_relation_type", ""))[:120],
        "product_or_service": str(raw.get("product_or_service", "")).strip()[:160],
        "evidence_quote": str(raw.get("evidence_quote", ""))[:700],
        "direction_assessment": str(raw.get("direction_assessment", "unclear")).lower() if str(raw.get("direction_assessment", "")).lower() in {"as_proposed", "reverse", "unclear"} else "unclear",
        "model_version": model_version,
    }


def build_direction_correction_proposals(relationships: list[dict[str, Any]], audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create auditable reverse-direction candidates; never auto-accept them."""
    relationship_by_id = {str(row.get("relationship_id", "")): row for row in relationships}
    proposals: list[dict[str, Any]] = []
    for audit in audits:
        if audit.get("direction_assessment") != "reverse":
            continue
        original = relationship_by_id.get(str(audit.get("relationship_id", "")))
        if not original:
            continue
        row = dict(original)
        source_id, source_name, source_role = row.get("target_entity_id"), row.get("target_entity_name"), row.get("target_role")
        target_id, target_name, target_role = row.get("source_entity_id"), row.get("source_entity_name"), row.get("source_role")
        fingerprint = f"direction-correction|{source_id}|{target_id}|{row.get('relationship_type')}|{row.get('modality')}"
        row.update({
            "relationship_id": f"rel:{sha256(fingerprint.encode()).hexdigest()[:16]}",
            "supplier_entity_id": source_id, "supplier_name": source_name,
            "customer_entity_id": target_id, "customer_name": target_name,
            "source_entity_id": source_id, "source_entity_name": source_name, "source_role": source_role,
            "target_entity_id": target_id, "target_entity_name": target_name, "target_role": target_role,
            "review_status": "needs_review", "decision": "review", "decision_source": "direction_correction_proposal",
            "decision_reason": f"Reverse-direction candidate proposed by evidence audit of {original.get('relationship_id')}: {audit.get('reason', '')}",
            "verification_status": "direction_correction_candidate",
            "risk_flags": sorted(set(row.get("risk_flags", [])) | {"llm_proposed_direction_correction"}),
            "direction_correction_of": original.get("relationship_id"),
        })
        proposals.append(row)
    return proposals


def normalize_follow_up(raw: Any) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    recommendation = str(raw.get("recommended_decision", "")).lower()
    if recommendation not in {"accept", "reject", "review"}:
        recommendation = ""
    return {
        "follow_up_reason": str(raw.get("follow_up_reason", ""))[:900],
        "missing_or_conflicting_evidence": str(raw.get("missing_or_conflicting_evidence", ""))[:700],
        "recommended_decision": recommendation,
    }


def merge_audit_history(audits: list[dict[str, Any]], prior_audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Show the latest valid review while retaining every audit event."""
    prior_by_id = {str(row.get("relationship_id", "")): row for row in prior_audits}
    for audit in audits:
        prior = prior_by_id.get(str(audit.get("relationship_id", "")), {})
        history = list(prior.get("decision_history", [])) or ([history_event(prior)] if prior else [])
        history.append(history_event(audit))
        audit["decision_history"] = history
        valid = [event for event in history if event.get("valid")]
        if valid:
            latest = valid[-1]
            audit["decision"] = latest["decision"]
            audit["reason"] = latest["reason"]
            audit["model_version"] = latest["model_version"]
            audit["current_reviewed_at"] = latest["reviewed_at"]
    return audits


def history_event(audit: dict[str, Any]) -> dict[str, Any]:
    reason = str(audit.get("reason", ""))
    follow_up = str(audit.get("follow_up_reason", ""))
    return {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "decision": str(audit.get("decision", "review")),
        "reason": reason,
        "model_version": str(audit.get("model_version", "")),
        "follow_up_reason": follow_up,
        "recommended_decision": str(audit.get("recommended_decision", "")),
        "valid": not reason.startswith("LLM error:") and not follow_up.startswith("Second-pass LLM error:"),
    }


def audit_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"relationship_count": len(rows), "decision_counts": dict(Counter(str(row["decision"]) for row in rows))}


def apply_latest_audit_decisions(relationships: list[dict[str, Any]], audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose the current valid audit conclusion without deleting raw evidence/history."""
    audits_by_id = {str(row.get("relationship_id", "")): row for row in audits}
    updated: list[dict[str, Any]] = []
    blocking_risks = {"direction_anomaly", "competitor_or_market_context", "cross_sentence_attachment_risk"}
    for relationship in relationships:
        row = dict(relationship)
        audit = audits_by_id.get(str(row.get("relationship_id", "")))
        # Historical cross-filing decisions made before risk flags existed must
        # not override a current direction/context warning. They need a fresh
        # evidence-grounded LLM audit instead.
        if audit and audit.get("decision_source") == "cross_filing_rule" and blocking_risks.intersection(set(row.get("risk_flags", []))):
            updated.append(row)
            continue
        if audit and audit.get("decision") in {"accept", "reject", "review"}:
            decision = str(audit["decision"])
            row["decision"] = decision
            row["decision_source"] = "llm_relationship_audit"
            row["decision_reason"] = audit.get("reason", "")
            row["review_status"] = {"accept": "accepted", "reject": "rejected", "review": "needs_review"}[decision]
            # Product/service is an evidence-grounded attribute supplied by the
            # auditor. Persist it on the canonical edge so a later refresh does
            # not erase it from the map.
            if product := str(audit.get("product_or_service", "")).strip():
                row["product_or_service"] = product
                row["risk_flags"] = [flag for flag in row.get("risk_flags", []) if flag != "product_or_service_not_extracted"]
        updated.append(row)
    return updated


def attach_audits_to_dashboard(dashboard_path: Path, audits: list[dict[str, Any]]) -> bool:
    """Attach audit results to a static dashboard payload without changing source evidence."""
    if not dashboard_path.exists():
        return False
    payload = json.loads(dashboard_path.read_text(encoding="utf-8"))
    by_relationship = {str(row["relationship_id"]): row for row in audits}
    for relationship in payload.get("canonical_relationships", []):
        audit = by_relationship.get(str(relationship.get("relationship_id", "")))
        if audit:
            relationship["llm_audit"] = audit
    payload["relationship_audit_summary"] = audit_summary(audits)
    dashboard_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return True


def attach_enrichment_to_dashboard(dashboard_path: Path, relationships: list[dict[str, Any]], audits: list[dict[str, Any]]) -> bool:
    if not dashboard_path.exists():
        return False
    payload = json.loads(dashboard_path.read_text(encoding="utf-8"))
    audit_by_id = {str(row["relationship_id"]): row for row in audits}
    existing_by_id = {str(row.get("relationship_id", "")): row for row in payload.get("canonical_relationships", [])}
    enriched: list[dict[str, Any]] = []
    for relationship in relationships:
        row = dict(relationship)
        audit = audit_by_id.get(str(row.get("relationship_id", "")), {})
        if product := str(audit.get("product_or_service", "")).strip():
            row["product_or_service"] = product
        row["llm_audit"] = audit
        existing = existing_by_id.get(str(row.get("relationship_id", "")), {})
        if existing.get("human_review"):
            row["human_review"] = existing["human_review"]
            row["review_status"] = existing.get("review_status", "unreviewed")
        enriched.append(row)
    payload["canonical_relationships"] = enriched
    payload["network_edges"] = canonical_network_edges(enriched)
    payload.setdefault("summary", {})["canonical_relationship_candidate_count"] = len(enriched)
    payload["summary"]["network_edge_count"] = len(payload["network_edges"])
    dashboard_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return True
