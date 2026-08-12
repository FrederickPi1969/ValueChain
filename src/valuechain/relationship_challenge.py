"""Evidence-bounded, user-facing questions about a graph connection."""

from __future__ import annotations

from typing import Any


SYSTEM_PROMPT = """You are a concise assistant helping a user understand one disclosed business relationship.
Use ONLY the supplied SEC evidence. Never use outside knowledge. Answer in plain language in at most 90 words.
Return JSON with exactly: answer, assessment, evidence_quote, needs_reaudit, re_audit_reason.
assessment is one of supported, concern, inconclusive.
Set needs_reaudit true only if the evidence directly contradicts the displayed direction/type, or only names the companies in a competitor/market/list context without stating the relationship. Do not change any database decision yourself."""


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
    return {
        "answer": str(raw.get("answer", "I could not determine this from the supplied evidence.")).strip()[:900],
        "assessment": assessment,
        "evidence_quote": str(raw.get("evidence_quote", "")).strip()[:700],
        "needs_reaudit": bool(raw.get("needs_reaudit", False)),
        "re_audit_reason": str(raw.get("re_audit_reason", "")).strip()[:900],
    }
