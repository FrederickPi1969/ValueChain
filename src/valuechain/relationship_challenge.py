"""Evidence-bounded, user-facing questions about a graph connection."""

from __future__ import annotations

import re
from typing import Any


SYSTEM_PROMPT = """You are an evidence-bounded assistant helping a user understand one disclosed business relationship.
Use ONLY the supplied SEC evidence. Never use outside knowledge.

The structured verdict (assessment) is the source of truth. Presentation text must never override or contradict it.
Return JSON with exactly: assessment, supporting_facts, rationale, evidence_quote, needs_reaudit, re_audit_reason.
assessment is one of supported, concern, inconclusive. supporting_facts is a list of at most two short facts stated in the filing. rationale is one short sentence explaining why those facts produce the assessment. Do not write an answer paragraph.
Set needs_reaudit true only if the evidence directly contradicts the displayed direction/type, or only names the companies in a competitor/market/list context without stating the relationship. Do not change any database decision yourself."""

REWRITE_PROMPT = """Rewrite a relationship explanation from a fixed structured verdict.
The assessment is the source of truth and must not be changed. Use only the supplied facts and rationale. Return JSON with exactly: answer. Write no more than two sentences and begin with the assessment label (Supported., Concern., or Inconclusive.)."""

NEGATIVE_CONCLUSIONS = re.compile(r"\b(?:does not support|not supported|wrong direction|incorrect|no evidence)\b", re.I)
CONCERN_SIGNALS = re.compile(r"\b(?:contradic\w*|conflict\w*|gap|does not|not mention|no evidence|wrong|incorrect|ambiguous|unclear)\b", re.I)


def challenge_payload(relationship: dict[str, Any], evidence: list[dict[str, Any]], question: str) -> dict[str, Any]:
    return {
        "displayed_connection": {
            "source": relationship.get("source_entity_name") or relationship.get("supplier_name") or relationship.get("object"),
            "target": relationship.get("target_entity_name") or relationship.get("customer_name") or relationship.get("subject"),
            "relation_type": relationship.get("relationship_type") or relationship.get("relation_type"),
            "product_or_service": relationship.get("product_or_service", ""),
        },
        "user_question": question or "Does this connection match the evidence?",
        "evidence": [
            {
                "form": row.get("form", ""), "section": row.get("source_section", ""),
                "text": str(row.get("evidence_text", ""))[:5000],
                "url": row.get("source_document_url", ""),
            }
            for row in evidence[:6]
        ],
    }


def normalize_challenge(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    assessment = str(raw.get("assessment", "inconclusive")).lower()
    if assessment not in {"supported", "concern", "inconclusive"}:
        assessment = "inconclusive"
    facts = normalize_facts(raw.get("supporting_facts"))
    rationale = str(raw.get("rationale", "")).strip()[:500]
    # New challenge responses deliberately have no free-form answer.  Keep the
    # legacy field only so old model payloads can be detected and repaired.
    answer = str(raw.get("answer", "")).strip()[:900] or render_explanation(assessment, facts, rationale)
    consistency = validate_explanation(assessment, answer)
    return {
        "answer": answer,
        "assessment": assessment,
        "supporting_facts": facts,
        "rationale": rationale,
        "evidence_quote": str(raw.get("evidence_quote", "")).strip()[:700],
        "needs_reaudit": bool(raw.get("needs_reaudit", False)),
        "re_audit_reason": str(raw.get("re_audit_reason", "")).strip()[:900],
        "explanation_inconsistent": not consistency["consistent"],
        "explanation_consistency_reason": consistency["reason"],
    }


def normalize_facts(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    return [str(item).strip()[:260] for item in values if str(item).strip()][:2]


def render_explanation(assessment: str, facts: list[str], rationale: str) -> str:
    label = {"supported": "Supported.", "concern": "Concern.", "inconclusive": "Inconclusive."}[assessment]
    # Presentation remains deterministic and capped at two sentences.  This
    # prevents a chatty model from reintroducing a conclusion after the verdict.
    clauses = [str(item).strip().rstrip(". ") for item in [*facts, rationale] if str(item).strip()]
    body = "; ".join(clauses)
    return f"{label} {body}.".strip() if body else label


def validate_explanation(assessment: str, explanation: str) -> dict[str, Any]:
    text = str(explanation or "").strip()
    if assessment == "supported" and NEGATIVE_CONCLUSIONS.search(text):
        return {"consistent": False, "reason": "supported_verdict_has_negative_conclusion"}
    if assessment == "concern" and not CONCERN_SIGNALS.search(text):
        return {"consistent": False, "reason": "concern_verdict_lacks_contradiction_or_gap"}
    return {"consistent": True, "reason": ""}


def rewrite_payload(challenge: dict[str, Any]) -> dict[str, Any]:
    return {
        "assessment": challenge["assessment"],
        "supporting_facts": challenge["supporting_facts"],
        "rationale": challenge["rationale"],
    }


def apply_explanation_rewrite(challenge: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Keep verdict fields immutable; repair presentation only."""
    raw = raw if isinstance(raw, dict) else {}
    answer = two_sentences(str(raw.get("answer", "")).strip()[:900])
    consistency = validate_explanation(challenge["assessment"], answer)
    if not answer or not consistency["consistent"]:
        answer = render_explanation(challenge["assessment"], challenge["supporting_facts"], challenge["rationale"])
        consistency = validate_explanation(challenge["assessment"], answer)
    return {
        **challenge,
        "answer": answer,
        "explanation_inconsistent": False,
        "explanation_consistency_reason": "rewritten_after_" + challenge["explanation_consistency_reason"],
        "explanation_rewritten": True,
    }


def two_sentences(value: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", value.strip())
    return " ".join(parts[:2]).strip()
