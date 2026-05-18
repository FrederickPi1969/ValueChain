from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from valuechain.aggregation import bottleneck_candidates
from valuechain.models import Company, FilingRecord, GraphEdge, Passage, RelationEvidence, SourceDocument


def render_dashboard(
    output_path: Path,
    edges: list[GraphEdge],
    evidence: list[RelationEvidence],
    yahoo_rows: list[dict] | None = None,
    companies: list[Company] | None = None,
    filings: list[FilingRecord] | None = None,
    source_documents: list[SourceDocument] | None = None,
    passages: list[Passage] | None = None,
    candidate_passages: list[Passage] | None = None,
) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template_dir = Path(__file__).resolve().parents[2] / "templates"
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("dashboard.html.j2")
    dashboard_data = build_dashboard_data(
        edges,
        evidence,
        yahoo_rows,
        companies,
        filings=filings,
        source_documents=source_documents,
        passages=passages,
        candidate_passages=candidate_passages,
    )
    output_path.write_text(
        template.render(
            edge_count=dashboard_data["summary"]["edge_count"],
            evidence_count=dashboard_data["summary"]["evidence_count"],
            company_count=dashboard_data["summary"]["company_count"],
            relation_mix=dashboard_data["relation_mix"],
            modality_mix=dashboard_data["modality_mix"],
            bottlenecks=dashboard_data["bottlenecks"],
            company_context=dashboard_data["companies"],
            edges=dashboard_data["edges"][:200],
            evidence=dashboard_data["evidence"],
            dashboard_data_json=json.dumps(dashboard_data, ensure_ascii=False),
        ),
        encoding="utf-8",
    )
    return dashboard_data


def build_dashboard_data(
    edges: list[GraphEdge],
    evidence: list[RelationEvidence],
    yahoo_rows: list[dict] | None = None,
    companies: list[Company] | None = None,
    filings: list[FilingRecord] | None = None,
    source_documents: list[SourceDocument] | None = None,
    passages: list[Passage] | None = None,
    candidate_passages: list[Passage] | None = None,
    company_activity: dict[str, dict[str, int]] | None = None,
) -> dict:
    evidence_by_company = Counter(record.subject for record in evidence)
    relation_mix = Counter(record.relation_type for record in evidence)
    modality_mix = Counter(record.modality for record in evidence)
    sorted_edges = sorted(edges, key=lambda edge: (-edge.evidence_count, edge.subject))
    evidence_rows = sorted(
        evidence,
        key=lambda record: (record.subject, record.relation_type, -record.confidence_score),
    )
    yahoo_by_symbol = {str(row.get("symbol", "")).upper(): row for row in yahoo_rows or []}
    company_context = build_company_context(
        edges,
        evidence,
        evidence_by_company,
        yahoo_by_symbol,
        companies,
        (
            company_activity
            if company_activity is not None
            else merge_activity(
                build_company_activity(filings, passages, candidate_passages),
                build_company_document_activity(source_documents),
            )
        ),
    )
    bottlenecks = bottleneck_candidates(edges)
    active_companies = {edge.subject for edge in edges} | {record.subject for record in evidence}
    universe_companies = {company.company_name for company in companies or []}
    dashboard_data = {
        "summary": {
            "edge_count": len(edges),
            "evidence_count": len(evidence),
            "company_count": len(universe_companies) if universe_companies else len(active_companies),
            "active_company_count": len(active_companies),
            "company_row_count": len(company_context),
            "bottleneck_count": len(bottlenecks),
            "source_document_count": len(source_documents or []),
            "exhibit_document_count": sum(1 for document in source_documents or [] if not document.is_primary),
        },
        "relation_mix": relation_mix.most_common(),
        "modality_mix": modality_mix.most_common(),
        "companies": company_context,
        "bottlenecks": bottlenecks,
        "edges": [edge.to_dict() for edge in sorted_edges],
        "evidence": [record.to_dict() for record in evidence_rows],
    }
    return dashboard_data


def build_company_context(
    edges: list[GraphEdge],
    evidence: list[RelationEvidence],
    evidence_by_company: Counter,
    yahoo_by_symbol: dict[str, dict],
    companies: list[Company] | None = None,
    company_activity: dict[str, dict[str, int]] | None = None,
) -> list[dict]:
    company_by_subject = {company.company_name: company for company in companies or []}
    subjects = sorted(
        {edge.subject for edge in edges}
        | {record.subject for record in evidence}
        | set(company_by_subject)
    )
    ticker_by_subject: dict[str, str] = {
        company.company_name: company.ticker for company in companies or []
    }
    for record in evidence:
        ticker_by_subject.setdefault(record.subject, record.ticker)
    exposures: dict[str, set[str]] = defaultdict(set)
    edge_counts: Counter = Counter()
    modality_counts: dict[str, Counter] = defaultdict(Counter)
    confidence_values: dict[str, list[float]] = defaultdict(list)
    for record in evidence:
        exposures[record.subject].add(record.relation_type)
        modality_counts[record.subject][record.modality] += 1
        confidence_values[record.subject].append(record.confidence_score)
    for edge in edges:
        exposures[edge.subject].add(edge.relation_type)
        edge_counts[edge.subject] += 1
    rows: list[dict] = []
    for subject in subjects:
        company = company_by_subject.get(subject)
        ticker = ticker_by_subject.get(subject, "")
        yahoo = yahoo_by_symbol.get(ticker.upper(), {})
        confidences = confidence_values.get(subject, [])
        activity = company_activity.get(subject, {}) if company_activity else {}
        filing_count = int(activity.get("filing_count", 0))
        source_document_count = int(activity.get("source_document_count", 0))
        exhibit_document_count = int(activity.get("exhibit_document_count", 0))
        passage_count = int(activity.get("passage_count", 0))
        candidate_count = int(activity.get("candidate_passage_count", 0))
        edge_count = edge_counts.get(subject, 0)
        evidence_count = evidence_by_company.get(subject, 0)
        rows.append(
            {
                "company": subject,
                "ticker": ticker,
                "role": company.role if company else "",
                "priority": company.priority if company else "",
                "exchange": company.exchange if company else "",
                "cik": company.cik if company else "",
                "notes": company.notes if company else "",
                "filing_count": filing_count,
                "source_document_count": source_document_count,
                "exhibit_document_count": exhibit_document_count,
                "passage_count": passage_count,
                "candidate_passage_count": candidate_count,
                "coverage_status": coverage_status(
                    filing_count,
                    candidate_count,
                    evidence_count,
                    edge_count,
                ),
                "evidence_count": evidence_count,
                "edge_count": edge_count,
                "risk_evidence_count": modality_counts[subject].get("risk_hypothetical", 0),
                "current_evidence_count": modality_counts[subject].get("current_fact", 0),
                "modality_counts": dict(modality_counts[subject]),
                "avg_confidence": round(sum(confidences) / max(len(confidences), 1), 3),
                "relation_types": ", ".join(sorted(exposures[subject])),
                "relation_type_count": len(exposures[subject]),
                "sector": yahoo.get("sector", ""),
                "industry": yahoo.get("industry", ""),
                "marketCap": yahoo.get("marketCap", ""),
            }
        )
    return sorted(rows, key=company_sort_key)


def build_company_activity(
    filings: list[FilingRecord] | None = None,
    passages: list[Passage] | None = None,
    candidate_passages: list[Passage] | None = None,
) -> dict[str, dict[str, int]]:
    activity: dict[str, dict[str, int]] = defaultdict(
        lambda: {"filing_count": 0, "passage_count": 0, "candidate_passage_count": 0}
    )
    for filing in filings or []:
        activity[filing.company_name]["filing_count"] += 1
    for passage in passages or []:
        activity[passage.company_name]["passage_count"] += 1
    for passage in candidate_passages or []:
        activity[passage.company_name]["candidate_passage_count"] += 1
    return dict(activity)


def build_company_document_activity(
    source_documents: list[SourceDocument] | None = None,
) -> dict[str, dict[str, int]]:
    activity: dict[str, dict[str, int]] = defaultdict(
        lambda: {"source_document_count": 0, "exhibit_document_count": 0}
    )
    for document in source_documents or []:
        activity[document.company_name]["source_document_count"] += 1
        if not document.is_primary:
            activity[document.company_name]["exhibit_document_count"] += 1
    return dict(activity)


def merge_activity(*activity_maps: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = defaultdict(dict)
    for activity_map in activity_maps:
        for company, values in activity_map.items():
            for key, value in values.items():
                merged[company][key] = int(merged[company].get(key, 0)) + int(value)
    return dict(merged)


def coverage_status(
    filing_count: int,
    candidate_count: int,
    evidence_count: int,
    edge_count: int,
) -> str:
    if edge_count:
        return "graph_ready"
    if evidence_count:
        return "evidence_only"
    if candidate_count:
        return "candidate_only"
    if filing_count:
        return "filed_no_candidates"
    return "no_filings"


def company_sort_key(row: dict) -> tuple[int, int, str]:
    priority = row.get("priority")
    try:
        priority_value = int(priority)
    except (TypeError, ValueError):
        priority_value = 999
    return (priority_value, -int(row["evidence_count"]), str(row["company"]))
