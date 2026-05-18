from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from valuechain.aggregation import bottleneck_candidates
from valuechain.models import GraphEdge, RelationEvidence


def render_dashboard(
    output_path: Path,
    edges: list[GraphEdge],
    evidence: list[RelationEvidence],
    yahoo_rows: list[dict] | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template_dir = Path(__file__).resolve().parents[2] / "templates"
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("dashboard.html.j2")
    evidence_by_company = Counter(record.subject for record in evidence)
    relation_mix = Counter(record.relation_type for record in evidence)
    modality_mix = Counter(record.modality for record in evidence)
    sorted_edges = sorted(edges, key=lambda edge: (-edge.evidence_count, edge.subject))
    evidence_rows = sorted(
        evidence,
        key=lambda record: (record.subject, record.relation_type, -record.confidence_score),
    )
    yahoo_by_symbol = {str(row.get("symbol", "")): row for row in yahoo_rows or []}
    company_context = build_company_context(edges, evidence, evidence_by_company, yahoo_by_symbol)
    bottlenecks = bottleneck_candidates(edges)
    dashboard_data = {
        "summary": {
            "edge_count": len(edges),
            "evidence_count": len(evidence),
            "company_count": len({edge.subject for edge in edges}),
            "bottleneck_count": len(bottlenecks),
        },
        "relation_mix": relation_mix.most_common(),
        "modality_mix": modality_mix.most_common(),
        "companies": company_context,
        "bottlenecks": bottlenecks,
        "edges": [edge.to_dict() for edge in sorted_edges],
        "evidence": [record.to_dict() for record in evidence_rows],
    }
    output_path.write_text(
        template.render(
            edge_count=len(edges),
            evidence_count=len(evidence),
            company_count=len({edge.subject for edge in edges}),
            relation_mix=relation_mix.most_common(),
            modality_mix=modality_mix.most_common(),
            bottlenecks=bottlenecks,
            company_context=company_context,
            edges=sorted_edges[:200],
            evidence=evidence_rows,
            dashboard_data_json=json.dumps(dashboard_data, ensure_ascii=False),
        ),
        encoding="utf-8",
    )


def build_company_context(
    edges: list[GraphEdge],
    evidence: list[RelationEvidence],
    evidence_by_company: Counter,
    yahoo_by_symbol: dict[str, dict],
) -> list[dict]:
    subjects = sorted({edge.subject for edge in edges})
    ticker_by_subject: dict[str, str] = {}
    for record in evidence:
        ticker_by_subject.setdefault(record.subject, record.ticker)
    exposures: dict[str, set[str]] = defaultdict(set)
    edge_counts: Counter = Counter()
    risk_counts: Counter = Counter()
    current_counts: Counter = Counter()
    avg_confidence: dict[str, list[float]] = defaultdict(list)
    for edge in edges:
        exposures[edge.subject].add(edge.relation_type)
        edge_counts[edge.subject] += 1
        avg_confidence[edge.subject].append(edge.avg_confidence)
        if edge.modality == "risk_hypothetical":
            risk_counts[edge.subject] += edge.evidence_count
        if edge.modality == "current_fact":
            current_counts[edge.subject] += edge.evidence_count
    rows: list[dict] = []
    for subject in subjects:
        ticker = ticker_by_subject.get(subject, "")
        yahoo = yahoo_by_symbol.get(ticker, {})
        confidences = avg_confidence.get(subject, [])
        rows.append(
            {
                "company": subject,
                "ticker": ticker,
                "evidence_count": evidence_by_company.get(subject, 0),
                "edge_count": edge_counts.get(subject, 0),
                "risk_evidence_count": risk_counts.get(subject, 0),
                "current_evidence_count": current_counts.get(subject, 0),
                "avg_confidence": round(sum(confidences) / max(len(confidences), 1), 3),
                "relation_types": ", ".join(sorted(exposures[subject])),
                "relation_type_count": len(exposures[subject]),
                "sector": yahoo.get("sector", ""),
                "industry": yahoo.get("industry", ""),
                "marketCap": yahoo.get("marketCap", ""),
            }
        )
    return sorted(rows, key=lambda row: -int(row["evidence_count"]))
