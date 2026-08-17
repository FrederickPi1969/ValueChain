"""Build a bounded, filing-grounded industry expansion artifact.

The extractor owns fact creation.  This module only traverses canonical
company-to-company facts, so expanding a graph cannot manufacture entities or
relationships that were not supported by a filing.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
import re
from typing import Iterable

from valuechain.models import Company


SCHEMA_VERSION = "industry-expansion/v1"
INDUSTRY_PATTERNS = {
    "semiconductor": re.compile(r"(?:semiconductor|foundry|chip|memory|semicap|accelerator|compute)", re.I),
    "cloud": re.compile(r"(?:cloud|hyperscaler|hosting|database|platform)", re.I),
    "data-center": re.compile(r"(?:data.?center|server|network|optical|colocation|cooling|power)", re.I),
    "datacenter": re.compile(r"(?:data.?center|server|network|optical|colocation|cooling|power)", re.I),
    "software": re.compile(r"(?:software|observability|application|platform)", re.I),
}


@dataclass(slots=True)
class ExpansionConfig:
    industry: str = "run-universe"
    seeds: list[str] = field(default_factory=list)
    max_hops: int = 2
    max_nodes: int = 1_500
    max_edges: int = 5_000
    max_seeds: int = 50
    forms: tuple[str, ...] = ("10-K", "10-Q")
    relationship_families: tuple[str, ...] = ("supply_chain",)
    include_candidates: bool = True


def build_industry_expansion(
    companies: list[Company],
    canonical_entities: list[dict[str, object]],
    canonical_relationships: list[dict[str, object]],
    config: ExpansionConfig | None = None,
) -> dict[str, object]:
    """Return a deterministic, bounded BFS expansion plus extraction plan."""

    config = config or ExpansionConfig()
    if config.max_hops < 0 or config.max_nodes < 1 or config.max_edges < 1:
        raise ValueError("max_hops must be non-negative and node/edge caps must be positive")

    entities = _entity_index(canonical_entities, canonical_relationships)
    company_by_name = {_key(row.company_name): row for row in companies}
    company_by_ticker = {_key(row.ticker): row for row in companies}
    company_by_entity: dict[str, Company] = {}
    for entity_id, row in entities.items():
        company = company_by_name.get(_key(str(row.get("canonical_name", ""))))
        if company:
            company_by_entity[entity_id] = company

    seed_ids, unresolved_seeds = _resolve_seeds(
        companies, entities, company_by_ticker, config.industry, config.seeds, config.max_seeds
    )
    eligible, exclusion_counts = _eligible_relationships(canonical_relationships, config)
    adjacency: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in eligible:
        adjacency[str(row["source_entity_id"])].append(row)
        adjacency[str(row["target_entity_id"])].append(row)
    for rows in adjacency.values():
        rows.sort(key=_edge_rank)

    selected = set(seed_ids)
    depth_by_id = {entity_id: 0 for entity_id in seed_ids}
    discovery: dict[str, dict[str, object]] = {}
    frontier = set(seed_ids)
    node_cap_reached = False
    for hop in range(1, config.max_hops + 1):
        next_frontier: set[str] = set()
        candidates: list[tuple[tuple[object, ...], str, str, dict[str, object]]] = []
        for parent_id in sorted(frontier):
            for row in adjacency.get(parent_id, []):
                source_id = str(row["source_entity_id"])
                target_id = str(row["target_entity_id"])
                neighbor_id = target_id if source_id == parent_id else source_id
                if neighbor_id in selected:
                    continue
                candidates.append((_edge_rank(row), parent_id, neighbor_id, row))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        for _, parent_id, neighbor_id, row in candidates:
            if neighbor_id in selected:
                continue
            if len(selected) >= config.max_nodes:
                node_cap_reached = True
                break
            selected.add(neighbor_id)
            next_frontier.add(neighbor_id)
            depth_by_id[neighbor_id] = hop
            discovery[neighbor_id] = {
                "parent_entity_id": parent_id,
                "relationship_id": row.get("relationship_id", ""),
                "evidence_ids": list(row.get("evidence_ids", [])),
                "source_accession_numbers": list(row.get("source_accession_numbers", [])),
            }
        frontier = next_frontier
        if not frontier or node_cap_reached:
            break

    induced = [
        row for row in eligible
        if str(row["source_entity_id"]) in selected and str(row["target_entity_id"]) in selected
    ]
    discovery_ids = {str(row.get("relationship_id", "")) for row in discovery.values()}
    induced.sort(key=lambda row: (0 if str(row.get("relationship_id", "")) in discovery_ids else 1, _edge_rank(row)))
    selected_relationships = induced[: config.max_edges]
    edge_cap_reached = len(induced) > config.max_edges

    incident: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in selected_relationships:
        incident[str(row["source_entity_id"])].append(row)
        incident[str(row["target_entity_id"])].append(row)

    nodes = [
        _node_row(
            entity_id, entities.get(entity_id, {}), company_by_entity.get(entity_id), depth_by_id[entity_id],
            entity_id in seed_ids, discovery.get(entity_id), incident.get(entity_id, []), config.forms,
        )
        for entity_id in selected
    ]
    nodes.sort(key=lambda row: (int(row["expansion_depth"]), str(row["canonical_name"])))
    edges = [_edge_row(row, depth_by_id) for row in selected_relationships]

    issuers_to_query = [
        {"ticker": row["ticker"], "company": row["canonical_name"], "forms": list(config.forms), "depth": row["expansion_depth"]}
        for row in nodes
        if row["ticker"] and row["expansion_depth"] > 0 and row["filing_status"] != "extracted"
    ]
    depth_counts = Counter(int(row["expansion_depth"]) for row in nodes)
    return {
        "schema_version": SCHEMA_VERSION,
        "config": asdict(config),
        "summary": {
            "seed_count": len(seed_ids), "node_count": len(nodes), "edge_count": len(edges),
            "accepted_edge_count": sum(1 for row in edges if row["review_status"] == "accepted"),
            "candidate_edge_count": sum(1 for row in edges if row["review_status"] != "accepted"),
            "max_depth_reached": max(depth_counts, default=0),
            "depth_counts": {str(key): value for key, value in sorted(depth_counts.items())},
            "node_cap_reached": node_cap_reached, "edge_cap_reached": edge_cap_reached,
            "eligible_relationship_count": len(eligible), "induced_relationship_count": len(induced),
        },
        "seed_entity_ids": seed_ids,
        "unresolved_seeds": unresolved_seeds,
        "nodes": nodes,
        "edges": edges,
        "extraction_plan": {
            "forms": list(config.forms),
            "already_extracted_issuers": sorted({name for row in selected_relationships for name in row.get("issuer_names", [])}),
            "next_hop_issuers_to_query": issuers_to_query,
        },
        "diagnostics": {
            "excluded_relationships": dict(sorted(exclusion_counts.items())),
            "stop_reason": "node_cap" if node_cap_reached else "frontier_exhausted",
            "edges_omitted_by_cap": max(0, len(induced) - len(edges)),
        },
    }


def _eligible_relationships(rows: Iterable[dict[str, object]], config: ExpansionConfig) -> tuple[list[dict[str, object]], Counter]:
    allowed_forms = {form.upper() for form in config.forms}
    allowed_families = set(config.relationship_families)
    eligible: list[dict[str, object]] = []
    excluded: Counter = Counter()
    for original in rows:
        row = _normalize_relationship(original)
        if not row:
            excluded["missing_endpoint"] += 1
            continue
        if row.get("review_status") == "rejected":
            excluded["rejected"] += 1
            continue
        if not config.include_candidates and row.get("review_status") != "accepted":
            excluded["candidate"] += 1
            continue
        if row.get("relationship_family", "supply_chain") not in allowed_families:
            excluded["relationship_family"] += 1
            continue
        source_types = {str(value).upper() for value in row.get("source_types", [])}
        if source_types and not source_types.intersection(allowed_forms):
            excluded["filing_form"] += 1
            continue
        eligible.append(row)
    return eligible, excluded


def _normalize_relationship(row: dict[str, object]) -> dict[str, object] | None:
    source_id = str(row.get("source_entity_id") or row.get("supplier_entity_id") or "")
    target_id = str(row.get("target_entity_id") or row.get("customer_entity_id") or "")
    source_name = str(row.get("source_entity_name") or row.get("supplier_name") or "").strip()
    target_name = str(row.get("target_entity_name") or row.get("customer_name") or "").strip()
    if not source_name or not target_name or source_name == target_name:
        return None
    source_id = source_id or f"entity:{_slug(source_name)}"
    target_id = target_id or f"entity:{_slug(target_name)}"
    return {**row, "source_entity_id": source_id, "target_entity_id": target_id,
            "source_entity_name": source_name, "target_entity_name": target_name}


def _entity_index(entities: list[dict[str, object]], relationships: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    index = {str(row.get("entity_id")): dict(row) for row in entities if row.get("entity_id")}
    for original in relationships:
        row = _normalize_relationship(original)
        if not row:
            continue
        for side in ("source", "target"):
            entity_id = str(row[f"{side}_entity_id"])
            index.setdefault(entity_id, {"entity_id": entity_id, "canonical_name": row[f"{side}_entity_name"], "entity_kind": "company", "role": ""})
    return index


def _resolve_seeds(
    companies: list[Company], entities: dict[str, dict[str, object]], company_by_ticker: dict[str, Company],
    industry: str, requested: list[str], max_seeds: int,
) -> tuple[list[str], list[str]]:
    entity_by_name = {_key(str(row.get("canonical_name", ""))): entity_id for entity_id, row in entities.items()}
    candidates = list(requested)
    if not candidates:
        preset_key = _key(industry)
        pattern = INDUSTRY_PATTERNS.get(preset_key) or INDUSTRY_PATTERNS.get(industry.casefold().strip())
        if pattern is None and industry and industry != "run-universe":
            pattern = re.compile(re.escape(industry), re.I)
        industry_companies = [row for row in companies if pattern and pattern.search(row.role)]
        candidates = [row.company_name for row in (industry_companies or companies)]
    resolved: list[str] = []
    unresolved: list[str] = []
    for value in candidates:
        company = company_by_ticker.get(_key(value))
        entity_id = entity_by_name.get(_key(company.company_name if company else value))
        if entity_id and entity_id not in resolved:
            resolved.append(entity_id)
        elif not entity_id:
            unresolved.append(value)
        if len(resolved) >= max_seeds:
            break
    return resolved, unresolved


def _node_row(entity_id: str, entity: dict[str, object], company: Company | None, depth: int, is_seed: bool,
              discovery: dict[str, object] | None, incident: list[dict[str, object]], forms: tuple[str, ...]) -> dict[str, object]:
    accessions = sorted({str(value) for row in incident for value in row.get("source_accession_numbers", []) if value})
    issuer_names = {_key(str(value)) for row in incident for value in row.get("issuer_names", [])}
    name = str(entity.get("canonical_name") or entity_id)
    extracted = _key(name) in issuer_names
    role = company.role if company else str(entity.get("role", ""))
    return {
        "entity_id": entity_id, "canonical_name": name,
        "ticker": company.ticker if company else str(entity.get("ticker", "")),
        "role": role, "industry_group": industry_group(role),
        "entity_kind": entity.get("entity_kind", "company"), "is_seed": is_seed,
        "is_universe_company": company is not None, "expansion_depth": depth,
        "discovered_from": discovery or {}, "filing_count": len(accessions),
        "filing_status": "extracted" if extracted else "query_available" if company else "external_unresolved",
        "requested_forms": list(forms), "degree": len(incident),
    }


def _edge_row(row: dict[str, object], depth_by_id: dict[str, int]) -> dict[str, object]:
    source_id, target_id = str(row["source_entity_id"]), str(row["target_entity_id"])
    review_status = str(row.get("review_status", "unreviewed"))
    return {
        "relationship_id": row.get("relationship_id", ""),
        "source_entity_id": source_id, "target_entity_id": target_id,
        "source": row["source_entity_name"], "target": row["target_entity_name"],
        # Backward-compatible map geometry.
        "object": row["source_entity_name"], "subject": row["target_entity_name"],
        "relation_type": row.get("relationship_type", "supplies_to"),
        "relationship_family": row.get("relationship_family", "supply_chain"),
        "modality": row.get("modality", "not_recorded"), "review_status": review_status,
        "confirmation_status": "confirmed" if review_status == "accepted" else "candidate",
        "verification_status": row.get("verification_status", "single_filing_candidate"),
        "confidence": row.get("confidence", 0), "evidence_count": row.get("evidence_count", 0),
        "evidence_ids": list(row.get("evidence_ids", [])),
        "source_accession_numbers": list(row.get("source_accession_numbers", [])),
        "source_types": list(row.get("source_types", [])), "issuer_names": list(row.get("issuer_names", [])),
        "product_or_service": row.get("product_or_service", ""), "risk_flags": list(row.get("risk_flags", [])),
        "decision_reason": row.get("decision_reason", ""), "decision_source": row.get("decision_source", ""),
        "llm_audit": row.get("llm_audit", {}),
        "expansion_depth": max(depth_by_id[source_id], depth_by_id[target_id]),
    }


def _edge_rank(row: dict[str, object]) -> tuple[object, ...]:
    return (
        0 if row.get("review_status") == "accepted" else 1,
        -int(row.get("evidence_count", 0)), -float(row.get("confidence", 0)),
        str(row.get("relationship_id", "")),
    )


def industry_group(role: str) -> str:
    value = role.casefold()
    if re.search(r"power|cooling|generation|grid", value):
        return "Power & thermal"
    if re.search(r"data.?center|colocation|server|network|optical|edge_cloud", value):
        return "Data-center infrastructure"
    if re.search(r"foundry|semiconductor|accelerator|memory|semicap|chip|compute", value):
        return "Semiconductors & compute"
    if re.search(r"cloud|hyperscaler|database|platform", value):
        return "Cloud & platforms"
    if re.search(r"software|observability|ai_software", value):
        return "AI software"
    return "External / unclassified"


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
