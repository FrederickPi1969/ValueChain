"""Safety Validation and decision engine for evidence-backed alias resolution."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


POLICY_VERSION = "alias-resolution-policy-v1"


def decide_alias_resolutions(
    candidates: list[dict[str, Any]], selections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return auto_accept, keep_unresolved, or human_review; never mutate entities.

    Hard checks establish candidate quality first.  The LLM may select among
    candidates but cannot promote a weak or conflicted match into auto-accept.
    """
    candidates_by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        candidates_by_query[str(row.get("query_object", ""))].append(row)
    selection_by_query = {str(row.get("query_object", "")): row for row in selections}
    results: list[dict[str, Any]] = []
    for query, group in candidates_by_query.items():
        group.sort(key=lambda row: int(row.get("candidate_rank") or 0))
        selection = selection_by_query.get(query, {})
        selected_rank = int(selection.get("selected_candidate_rank") or 0)
        selected = next((row for row in group if int(row.get("candidate_rank") or 0) == selected_rank), None)
        llm_decision = str(selection.get("decision") or "ambiguous")
        assessments = selection.get("candidate_assessments") or []
        selected_assessment = next((row for row in assessments if int(row.get("candidate_rank") or 0) == selected_rank), {})
        selected_match = str(selected_assessment.get("assessment") or "").upper() == "MATCH"
        alternative_uncertain = any(
            int(row.get("candidate_rank") or 0) != selected_rank and str(row.get("assessment") or "").upper() != "NO_MATCH"
            for row in assessments
        )
        validation = safety_validation(selected, group)
        conflict = has_material_conflict(selected, group)
        llm_confidence = float(selection.get("llm_confidence") or 0.0)
        if llm_decision == "no_match":
            decision, reason = "KEEP_UNRESOLVED", "LLM judged that none of the supplied evidence-backed candidates is the same legal entity."
        elif llm_decision == "select" and selected_match and not alternative_uncertain and validation["passed"] and llm_confidence >= 0.85 and not conflict:
            decision, reason = "AUTO_ACCEPT", "LLM MATCH passed Safety Validation with no material candidate conflict."
        else:
            decision = "REVIEW"
            reason = "LLM assessment is uncertain or Safety Validation found insufficient evidence/conflict."
        results.append({
            "query_object": query,
            "policy_version": POLICY_VERSION,
            "decision": decision,
            "selected_candidate_rank": selected_rank if selected else 0,
            "selected_lei": selected.get("lei", "") if selected else "",
            "selected_canonical_name": selected.get("canonical_name", "") if selected else "",
            "llm_decision": llm_decision,
            "llm_confidence": llm_confidence,
            "llm_reason": selection.get("llm_reason", ""),
            "llm_candidate_assessments": assessments,
            "safety_validation_status": "PASS" if validation["passed"] else "REVIEW",
            "safety_validation_reason": validation["reason"],
            "candidate_conflict": conflict,
            "policy_reason": reason,
        })
    return results


def safety_validation(selected: dict[str, Any] | None, candidates: list[dict[str, Any]]) -> dict[str, object]:
    if not selected:
        return {"passed": False, "reason": "No selected candidate."}
    if int(selected.get("candidate_rank") or 0) != 1:
        return {"passed": False, "reason": "Selected candidate is not the top-ranked evidence-backed candidate."}
    if float(selected.get("resolver_confidence") or 0) < 0.95:
        return {"passed": False, "reason": "Resolver confidence is below 0.95."}
    if float(selected.get("name_similarity") or 0) < 0.96:
        return {"passed": False, "reason": "Legal-name similarity is below 0.96."}
    if selected.get("entity_status") not in {"", "ACTIVE"}:
        return {"passed": False, "reason": "External legal-entity record is not active."}
    if selected.get("registration_status") not in {"", "ISSUED"}:
        return {"passed": False, "reason": "External legal-entity registration is not issued."}
    return {"passed": True, "reason": "Rank-1 active legal entity has an exact/near-exact legal-name match."}


def has_material_conflict(selected: dict[str, Any] | None, candidates: list[dict[str, Any]]) -> bool:
    if not selected or not selected.get("lei"):
        return False
    for candidate in candidates:
        if candidate.get("lei") == selected.get("lei"):
            continue
        if float(candidate.get("resolver_confidence") or 0) >= 0.90 and float(candidate.get("name_similarity") or 0) >= 0.92:
            return True
    return False
