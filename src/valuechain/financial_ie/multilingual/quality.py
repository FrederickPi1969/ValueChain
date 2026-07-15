from __future__ import annotations

from collections import Counter
from typing import Any

from valuechain.financial_ie.multilingual.languages import PACKS


def audit_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    identity = record.get("identity") or {}
    language = str(identity.get("language") or "")
    filing_id = str(identity.get("filing_id") or "")
    if record.get("status") != "complete":
        return [
            _issue(record, "error", "document_failed", str(record.get("error") or "unknown error"))
        ]
    for warning in record.get("diagnostics", {}).get("parser_warnings", []):
        issues.append(_issue(record, "warning", "parser_warning", str(warning)))
    if record.get("diagnostics", {}).get("chunk_count", 0) == 0:
        issues.append(_issue(record, "error", "empty_source_text", "No text chunks were parsed"))
    source_ratio = float(record.get("diagnostics", {}).get("source_native_script_ratio") or 0)
    if source_ratio < 0.15:
        issues.append(
            _issue(
                record,
                "warning",
                "low_native_script_ratio",
                f"Native-script ratio is {source_ratio:.3f} for {language}",
            )
        )
    profile = record.get("profile") or {}
    if profile.get("business_summary_native") and not profile.get("evidence"):
        issues.append(
            _issue(record, "error", "uncited_profile", "Profile summary has no source evidence")
        )
    for item in profile.get("evidence", []):
        if not item.get("evidence_valid"):
            issues.append(
                _issue(
                    record,
                    "error",
                    "invalid_profile_evidence",
                    str(item.get("evidence_failure_reason") or filing_id),
                )
            )
    pack = PACKS.get(language)
    for kind in ("signals", "relations"):
        for row in record.get(kind, []):
            if not row.get("evidence_valid"):
                issues.append(
                    _issue(
                        record,
                        "error",
                        f"invalid_{kind[:-1]}_evidence",
                        str(row.get("evidence_failure_reason") or "evidence failed"),
                    )
                )
            if not row.get("modality"):
                issues.append(
                    _issue(record, "error", f"missing_{kind[:-1]}_modality", "No valid modality")
                )
            if kind == "relations" and row.get("semantic_warning"):
                issues.append(
                    _issue(
                        record,
                        "warning",
                        "relation_semantic_guard",
                        str(row.get("semantic_warning")),
                    )
                )
            if kind == "relations" and not row.get("direction"):
                issues.append(
                    _issue(
                        record,
                        "error",
                        "missing_relation_direction",
                        "No valid semantic direction",
                    )
                )
            quote = str(row.get("evidence_quote_native") or "")
            if (
                pack
                and row.get("modality") == "current_fact"
                and any(marker in quote for marker in pack.hypothetical_markers)
            ):
                issues.append(
                    _issue(
                        record,
                        "warning",
                        "possible_hypothetical_as_current",
                        quote[:240],
                    )
                )
    if identity.get("document_granularity") == "event_disclosure":
        if not record.get("signals") and not record.get("relations"):
            issues.append(
                _issue(
                    record,
                    "info",
                    "empty_event_extraction",
                    "Event may be immaterial to the experiment schema",
                )
            )
    elif not record.get("signals"):
        issues.append(
            _issue(record, "warning", "empty_periodic_signals", "Periodic filing produced no signals")
        )
    return issues


def summarize(records: list[dict[str, Any]], issues: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [record for record in records if record.get("status") == "complete"]
    per_language: dict[str, dict[str, Any]] = {}
    for language in sorted(
        {str((record.get("identity") or {}).get("language") or "unknown") for record in records}
    ):
        rows = [
            record
            for record in records
            if str((record.get("identity") or {}).get("language") or "unknown") == language
        ]
        per_language[language] = _summary_block(rows)
    issue_counts = Counter(f"{row['severity']}:{row['code']}" for row in issues)
    return {
        "schema_version": "multilingual-financial-ie-v0.3",
        "documents_requested": len(records),
        "documents_complete": len(complete),
        "documents_failed": len(records) - len(complete),
        "overall": _summary_block(records),
        "per_language": per_language,
        "quality_issue_counts": dict(sorted(issue_counts.items())),
        "database_writes": 0,
        "production_tables_touched": [],
    }


def review_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        identity = record.get("identity") or {}
        chunks = {
            str(row.get("chunk_id")): str(row.get("text") or "")
            for row in record.get("evidence_chunks", [])
        }
        profile = record.get("profile") or {}
        for item in profile.get("evidence", []):
            rows.append(
                _review_row(identity, "profile", "business_profile", item, chunks)
            )
        for item in record.get("signals", []):
            rows.append(
                _review_row(identity, "signal", str(item.get("category") or ""), item, chunks)
            )
        for item in record.get("relations", []):
            rows.append(
                _review_row(
                    identity,
                    "relation",
                    str(item.get("relation_type") or ""),
                    item,
                    chunks,
                )
            )
    rows.sort(key=lambda row: (row["evidence_valid"] == "true", row["language"], row["filing_id"]))
    return rows


def _summary_block(records: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [record for record in records if record.get("status") == "complete"]
    signals = [item for record in complete for item in record.get("signals", [])]
    relations = [item for record in complete for item in record.get("relations", [])]
    profile_evidence = [
        item
        for record in complete
        for item in (record.get("profile") or {}).get("evidence", [])
    ]
    all_evidence = [*profile_evidence, *signals, *relations]
    valid = sum(bool(item.get("evidence_valid")) for item in all_evidence)
    return {
        "documents": len(records),
        "complete": len(complete),
        "signals": len(signals),
        "relations": len(relations),
        "graph_ready_relations": sum(
            item.get("review_status") == "candidate" for item in relations
        ),
        "documents_with_signals": sum(bool(record.get("signals")) for record in complete),
        "documents_with_relations": sum(bool(record.get("relations")) for record in complete),
        "evidence_items": len(all_evidence),
        "evidence_valid": valid,
        "evidence_exact_rate": round(valid / len(all_evidence), 4) if all_evidence else None,
        "mean_source_native_script_ratio": round(
            sum(
                float(record.get("diagnostics", {}).get("source_native_script_ratio") or 0)
                for record in complete
            )
            / len(complete),
            4,
        )
        if complete
        else None,
    }


def _review_row(
    identity: dict[str, Any],
    kind: str,
    category: str,
    item: dict[str, Any],
    chunks: dict[str, str],
) -> dict[str, Any]:
    chunk_id = str(item.get("chunk_id") or "")
    quote = str(item.get("evidence_quote_native") or item.get("quote_native") or "")
    return {
        "source_id": identity.get("source_id"),
        "language": identity.get("language"),
        "issuer_name": identity.get("issuer_name"),
        "filing_id": identity.get("filing_id"),
        "filing_type": identity.get("filing_type"),
        "item_kind": kind,
        "category": category,
        "modality": item.get("modality"),
        "direction": item.get("direction"),
        "chunk_id": chunk_id,
        "evidence_quote_native": quote,
        "source_chunk": chunks.get(chunk_id, ""),
        "evidence_valid": str(bool(item.get("evidence_valid"))).lower(),
        "evidence_failure_reason": item.get("evidence_failure_reason"),
        "source_url": identity.get("source_url"),
        "human_label": "",
        "review_notes": "",
    }


def _issue(record: dict[str, Any], severity: str, code: str, detail: str) -> dict[str, Any]:
    identity = record.get("identity") or {}
    return {
        "severity": severity,
        "code": code,
        "source_id": identity.get("source_id"),
        "language": identity.get("language"),
        "issuer_name": identity.get("issuer_name"),
        "filing_id": identity.get("filing_id"),
        "detail": detail,
    }
