from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


CORE_FACTS = ("revenue", "net_income", "total_assets", "operating_cash_flow")
RISK_LANGUAGE = re.compile(r"\b(may|might|could|would|potential(?:ly)?|if)\b", re.IGNORECASE)


def audit_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    issues = [issue for record in records for issue in audit_company_record(record)]
    counts = Counter(str(issue["issue_type"]) for issue in issues)
    severity_counts = Counter(str(issue["severity"]) for issue in issues)
    return issues, {
        "quality_issue_count": len(issues),
        "quality_issue_types": dict(sorted(counts.items())),
        "quality_issue_severities": dict(sorted(severity_counts.items())),
        "companies_with_quality_issues": len({str(issue["ticker"]) for issue in issues}),
    }


def audit_company_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    if record.get("status") != "complete":
        return [make_issue(record, "incomplete_company", "error", str(record.get("error") or record.get("status")))]
    issues: list[dict[str, Any]] = []
    facts = {str(fact.get("field")): fact for fact in record.get("financial_facts", [])}
    for field in CORE_FACTS:
        if field not in facts:
            issues.append(make_issue(record, "missing_core_fact", "warning", field))
    issues.extend(audit_fact_values(record, facts))
    profile = record.get("profile", {})
    if not profile.get("evidence_valid"):
        issues.append(make_issue(record, "profile_evidence_unverified", "warning", "One or more profile quotes failed validation"))
    if int(profile.get("strategic_importance") or 1) >= 4 and not profile.get("evidence_valid"):
        issues.append(make_issue(record, "high_importance_profile_unverified", "error", str(profile.get("strategic_importance"))))
    seen_signals: set[tuple[str, str]] = set()
    for signal in record.get("material_signals", []):
        headline = normalize_text(str(signal.get("headline") or signal.get("statement") or ""))
        key = str(signal.get("category") or ""), headline
        if key in seen_signals:
            issues.append(make_issue(record, "duplicate_signal", "warning", headline, signal))
        seen_signals.add(key)
        if not signal.get("evidence_valid"):
            severity = "error" if int(signal.get("significance") or 1) >= 4 else "warning"
            detail = f"{signal.get('evidence_failure_reason') or 'unverified'}:{headline}"
            issues.append(make_issue(record, "signal_evidence_unverified", severity, detail, signal))
        quote = str(signal.get("evidence_quote") or "")
        if signal.get("modality") == "current_fact" and RISK_LANGUAGE.search(quote):
            issues.append(make_issue(record, "current_fact_contains_risk_language", "warning", quote[:240], signal))
    diagnostics = record.get("diagnostics", {})
    if diagnostics.get("parser_warnings"):
        issues.append(
            make_issue(
                record,
                "parser_fallback_or_warning",
                "warning",
                " | ".join(map(str, diagnostics.get("parser_warnings", []))),
            )
        )
    if str(diagnostics.get("signal_parse_error") or "").startswith("partial_array_recovery:"):
        issues.append(make_issue(record, "partial_signal_json_recovery", "warning", diagnostics["signal_parse_error"]))
    return issues


def audit_fact_values(
    record: dict[str, Any],
    facts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for field, fact in facts.items():
        value = decimal_value(fact.get("value"))
        if value is None:
            issues.append(make_issue(record, "invalid_fact_number", "error", field))
            continue
        if field in {"total_assets", "total_liabilities"} and value < 0:
            issues.append(make_issue(record, "negative_balance_sheet_fact", "error", f"{field}={value}"))
        if fact.get("period_type") == "duration":
            start = iso_date(fact.get("period_start"))
            end = iso_date(fact.get("period_end"))
            if start and end and not 300 <= (end - start).days <= 430:
                issues.append(
                    make_issue(record, "non_annual_duration_fact", "error", f"{field}:{start}/{end}")
                )
    assets = decimal_value(facts.get("total_assets", {}).get("value"))
    liabilities = decimal_value(facts.get("total_liabilities", {}).get("value"))
    equity = decimal_value(facts.get("stockholders_equity", {}).get("value"))
    if assets is not None and liabilities is not None and equity is not None and assets:
        mismatch = abs(assets - liabilities - equity) / abs(assets)
        if mismatch > Decimal("0.05"):
            issues.append(
                make_issue(
                    record,
                    "accounting_identity_mismatch",
                    "error" if mismatch > Decimal("0.50") else "warning",
                    f"relative_mismatch={mismatch:.4f}",
                )
            )
    return issues


def make_issue(
    record: dict[str, Any],
    issue_type: str,
    severity: str,
    detail: str,
    signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signal = signal or {}
    return {
        "ticker": record.get("ticker"),
        "company_name": record.get("company_name"),
        "issue_type": issue_type,
        "severity": severity,
        "detail": detail,
        "signal_category": signal.get("category"),
        "signal_headline": signal.get("headline"),
        "chunk_id": signal.get("chunk_id"),
        "accession_number": record.get("accession_number"),
        "source_document_url": record.get("source_document_url"),
    }


def write_quality_issues(path: Path, issues: list[dict[str, Any]]) -> None:
    fields = [
        "ticker",
        "company_name",
        "issue_type",
        "severity",
        "detail",
        "signal_category",
        "signal_headline",
        "chunk_id",
        "accession_number",
        "source_document_url",
        "human_disposition",
        "review_notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(issues)


def decimal_value(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
