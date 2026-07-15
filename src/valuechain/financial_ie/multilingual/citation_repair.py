from __future__ import annotations

from typing import Any

from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.multilingual.evidence import normalize_match_text, quote_in_text


def collect_repair_items(
    profile: dict[str, Any],
    signals: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    chunks: dict[str, DocumentChunk],
    *,
    limit: int = 12,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, row in enumerate(profile.get("evidence", [])):
        _add_item(
            items,
            f"profile:{index}",
            str(profile.get("business_summary_native") or ""),
            row,
            chunks,
        )
    for index, row in enumerate(signals):
        _add_item(
            items,
            f"signal:{index}",
            str(row.get("statement_native") or ""),
            row,
            chunks,
        )
    for index, row in enumerate(relations):
        claim = " | ".join(
            str(row.get(key) or "")
            for key in ("subject_native", "relation_type", "object_native")
        )
        _add_item(items, f"relation:{index}", claim, row, chunks)
    return items[:limit]


def apply_repairs(
    payload: Any,
    profile: dict[str, Any],
    signals: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    chunks: dict[str, DocumentChunk],
) -> dict[str, int]:
    rows = payload.get("repairs", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {"requested": 0, "accepted": 0, "rejected": 0}
    targets: dict[str, dict[str, Any]] = {}
    targets.update({f"profile:{index}": row for index, row in enumerate(profile.get("evidence", []))})
    targets.update({f"signal:{index}": row for index, row in enumerate(signals)})
    targets.update({f"relation:{index}": row for index, row in enumerate(relations)})
    accepted = 0
    rejected = 0
    for repair in rows:
        if not isinstance(repair, dict):
            rejected += 1
            continue
        item_id = str(repair.get("item_id") or "")
        target = targets.get(item_id)
        chunk_id = str(repair.get("chunk_id") or "")
        quote = str(repair.get("quote_native") or "").strip()
        chunk = chunks.get(chunk_id)
        if (
            not target
            or chunk_id != str(target.get("chunk_id") or "")
            or not chunk
            or len(normalize_match_text(quote)) < 8
            or not quote_in_text(quote, chunk.text)
            or not _relation_object_supported(item_id, target, quote)
        ):
            rejected += 1
            continue
        quote_key = "quote_native" if item_id.startswith("profile:") else "evidence_quote_native"
        target[f"{quote_key}_original"] = target.get(quote_key)
        target[quote_key] = quote
        target["evidence_valid"] = True
        target["evidence_failure_reason"] = ""
        target["evidence_repaired"] = True
        if item_id.startswith("relation:"):
            from valuechain.financial_ie.multilingual.schema import relation_semantic_warning

            target["semantic_warning"] = relation_semantic_warning(
                str(target.get("relation_type") or ""),
                str(target.get("object_native") or ""),
                quote,
                str(target.get("direction") or "") or None,
            )
        if "review_status" in target:
            direction_valid = not item_id.startswith("relation:") or bool(
                target.get("direction")
            )
            target["review_status"] = (
                "candidate"
                if target.get("modality")
                and direction_valid
                and not target.get("semantic_warning")
                else "needs_review"
            )
        accepted += 1
    profile["evidence_valid"] = all(
        item.get("evidence_valid") for item in profile.get("evidence", [])
    )
    return {"requested": len(rows), "accepted": accepted, "rejected": rejected}


def _add_item(
    items: list[dict[str, str]],
    item_id: str,
    claim: str,
    row: dict[str, Any],
    chunks: dict[str, DocumentChunk],
) -> None:
    if row.get("evidence_valid"):
        return
    chunk_id = str(row.get("chunk_id") or "")
    chunk = chunks.get(chunk_id)
    if not chunk:
        return
    items.append(
        {
            "item_id": item_id,
            "chunk_id": chunk_id,
            "claim_native": claim,
            "failed_quote": str(
                row.get("evidence_quote_native") or row.get("quote_native") or ""
            ),
            "source_chunk": chunk.text,
        }
    )


def _relation_object_supported(
    item_id: str,
    target: dict[str, Any],
    quote: str,
) -> bool:
    if not item_id.startswith("relation:"):
        return True
    object_name = normalize_match_text(str(target.get("object_native") or ""))
    return not object_name or object_name in normalize_match_text(quote)
