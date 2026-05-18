from __future__ import annotations

from collections import defaultdict

from valuechain.models import GraphEdge, RelationEvidence


def aggregate_edges(records: list[RelationEvidence]) -> list[GraphEdge]:
    groups: dict[tuple[str, str, str, str], list[RelationEvidence]] = defaultdict(list)
    for record in records:
        key = (
            record.subject.strip(),
            record.object.strip(),
            record.relation_type,
            record.modality,
        )
        groups[key].append(record)

    edges: list[GraphEdge] = []
    for (subject, obj, relation_type, modality), group in groups.items():
        filing_dates = sorted({record.filing_date for record in group if record.filing_date})
        confidences = [record.confidence_score for record in group]
        edges.append(
            GraphEdge(
                subject=subject,
                object=obj,
                relation_type=relation_type,
                modality=modality,
                first_seen=filing_dates[0] if filing_dates else "",
                last_seen=filing_dates[-1] if filing_dates else "",
                evidence_count=len(group),
                avg_confidence=round(sum(confidences) / max(len(confidences), 1), 3),
                forms=";".join(sorted({record.form for record in group})),
                accessions=";".join(sorted({record.accession_number for record in group})),
                source_urls=";".join(sorted({record.source_document_url for record in group})),
            )
        )
    return sorted(edges, key=lambda edge: (-edge.evidence_count, edge.subject, edge.relation_type))


def bottleneck_candidates(edges: list[GraphEdge], min_subjects: int = 2) -> list[dict[str, object]]:
    by_object: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in edges:
        by_object[edge.object].append(edge)
    rows: list[dict[str, object]] = []
    for obj, obj_edges in by_object.items():
        subjects = sorted({edge.subject for edge in obj_edges})
        if len(subjects) < min_subjects and len(obj_edges) < 2:
            continue
        rows.append(
            {
                "object": obj,
                "dependent_company_count": len(subjects),
                "evidence_count": sum(edge.evidence_count for edge in obj_edges),
                "relation_types": ";".join(sorted({edge.relation_type for edge in obj_edges})),
                "subjects": ";".join(subjects[:12]),
            }
        )
    return sorted(rows, key=lambda row: (-int(row["dependent_company_count"]), -int(row["evidence_count"])))

