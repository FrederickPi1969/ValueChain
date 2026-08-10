"""Turn evidence-backed issuer relationships into an auditable supply graph.

Raw evidence always preserves the issuer-centric extraction orientation. This
module makes a separate, conservative canonical layer: only resolved corporate
counterparties and supply-relevant relation types create supplier-to-customer
edges. All other records become diagnostics rather than silently disappearing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from hashlib import sha256
import re

from valuechain.edge_quality import normalize_dependency_object, object_key
from valuechain.evidence_identity import stable_evidence_id
from valuechain.models import Company, RelationEvidence
from valuechain.ontology import blocking_risk_flags, canonical_relation_for_raw, category_for_raw, orientation_for_raw, raw_relation_spec


# A supply relationship has exactly one graph meaning: supplier -> customer.
# Extraction categories describe *what* is supplied and remain attributes.
# These are filing-table headings, not legal entities. Do not include legal
# suffix-like strings here: a plausible company must remain resolvable.
NON_ENTITY_BLOCKLIST = {
    "design businesses",
    "domestic subsidiaries",
    "foreign subsidiaries",
    "networks",
    "engineering",
    "service",
    "businesses",
    "subsidiaries",
}


def build_canonical_layer(
    companies: list[Company], evidence: list[RelationEvidence]
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Return canonical entities, supply relationships, and per-evidence diagnostics."""

    entities: dict[str, dict[str, object]] = {}
    for company in companies:
        add_entity(
            entities,
            company.company_name,
            ticker=company.ticker,
            cik=company.cik,
            role=company.role,
            resolution_status="universe_resolved",
            entity_kind="company",
        )

    groups: dict[tuple[str, str, str, str, str], list[RelationEvidence]] = defaultdict(list)
    diagnostics: list[dict[str, object]] = []
    for record in evidence:
        flags = relation_risk_flags(record)
        record = replace(record, risk_flags=sorted(set(record.risk_flags) | flags))
        subject_id = add_entity(
            entities,
            record.subject,
            ticker=record.ticker,
            cik=record.cik,
            resolution_status="issuer_resolved",
            entity_kind="company",
        )
        object_info = normalize_dependency_object(record.object, subject=record.subject, evidence_text=record.evidence_text)
        if is_non_entity_fragment(object_info.display_name):
            diagnostics.append(diagnostic(record, "non_entity_blocklisted", object_info.display_name))
            continue
        if object_info.is_generic or object_info.object_kind not in {"company", "organization"}:
            diagnostics.append(diagnostic(record, "unresolved_or_generic_counterparty", object_info.display_name))
            continue
        relation_info = canonical_relation(record.relation_type)
        if not relation_info:
            diagnostics.append(diagnostic(record, "not_a_canonical_relationship", object_info.display_name))
            continue
        relationship_family, relation = relation_info
        object_id = add_entity(
            entities,
            object_info.display_name,
            resolution_status="alias_resolved" if object_info.object_kind == "company" else "name_resolved",
            entity_kind=object_info.object_kind,
        )
        source_id, target_id = orient_relation(record.relation_type, subject_id, object_id)
        groups[canonical_merge_key(source_id, target_id, relation, record.modality, record.product_or_service, relationship_family)].append(record)
        diagnostics.append(diagnostic(record, "canonicalized", object_info.display_name))

    relationships = [relationship_from_group(key, rows, entities) for key, rows in groups.items()]
    apply_cross_filing_verification(relationships)
    apply_parentage(entities, relationships)
    relationships.sort(key=lambda row: (-int(row["evidence_count"]), str(row["supplier_name"]), str(row["customer_name"])))
    return sorted(entities.values(), key=lambda row: str(row["canonical_name"])), relationships, diagnostics


def canonical_relation(relation_type: str) -> tuple[str, str] | None:
    return canonical_relation_for_raw(relation_type)


def relation_risk_flags(record: RelationEvidence) -> set[str]:
    """Evidence-risk labels. They never rewrite endpoints or truth values."""
    text = record.evidence_text.casefold()
    flags: set[str] = set()
    if any(marker in text for marker in ("competitor", "competition", "compete with")):
        flags.add("competitor_or_market_context")
    if record.relation_type in {"data_center_dependency", "cloud_or_hosting_dependency"} and any(marker in text for marker in ("each customer", "customer intends", "being deployed by", "deployed by")):
        flags.add("direction_anomaly")
    # Multiple sentences are valid evidence, but named entities can be attached
    # to the wrong trigger; require the auditor to inspect scope explicitly.
    if len([part for part in text.replace(";", ".").split(".") if part.strip()]) > 1:
        flags.add("cross_sentence_attachment_risk")
    if (raw_relation_spec(record.relation_type) or {}).get("canonical_type") in {"supplies_to", "licenses_to"} and not record.product_or_service:
        flags.add("product_or_service_not_extracted")
    return flags


def canonical_merge_key(
    source_id: str, target_id: str, relationship_type: str, modality: str,
    product_or_service: str = "", relationship_family: str = "supply_chain",
) -> tuple[str, str, str, str, str]:
    """Stable conservative merge key for evidence-backed canonical facts.

    Product/service is an evidence-backed attribute, not part of the identity
    of a business fact.  Keeping it out of the key means later extraction
    enrichment cannot replace an accepted relationship with a new ID.
    """
    del product_or_service  # Backward-compatible parameter; attributes do not define identity.
    return (source_id, target_id, relationship_type, modality, relationship_family)


def orient_relation(relation_type: str, subject_id: str, object_id: str) -> tuple[str, str]:
    return (subject_id, object_id) if orientation_for_raw(relation_type) == "subject_to_object" else (object_id, subject_id)


def add_entity(
    entities: dict[str, dict[str, object]],
    name: str,
    *,
    ticker: str = "",
    cik: str = "",
    role: str = "",
    resolution_status: str,
    entity_kind: str,
) -> str:
    entity_id = f"entity:{object_key(name)}"
    current = entities.get(entity_id)
    row = {
        "entity_id": entity_id,
        "canonical_name": name,
        "ticker": ticker,
        "cik": cik,
        "role": role,
        "entity_kind": entity_kind,
        "resolution_status": resolution_status,
    }
    if current is None or current["resolution_status"] not in {"universe_resolved", "issuer_resolved"}:
        entities[entity_id] = row
    return entity_id


def relationship_from_group(
    key: tuple[str, str, str, str, str], rows: list[RelationEvidence], entities: dict[str, dict[str, object]]
) -> dict[str, object]:
    supplier_id, customer_id, relationship_type, modality, relationship_family = key
    product_or_service = selected_product_or_service(rows)
    source_role, target_role = endpoint_roles(relationship_family, relationship_type)
    evidence_ids = sorted({stable_evidence_id(row) for row in rows})
    dates = sorted({row.filing_date for row in rows if row.filing_date})
    # Keep the historical empty-product fingerprint.  Old accepted IDs remain
    # valid while product/service can be enriched as an attribute.
    fingerprint = "|".join([supplier_id, customer_id, relationship_type, modality, "", relationship_family])
    return {
        "relationship_id": f"rel:{sha256(fingerprint.encode()).hexdigest()[:16]}",
        "supplier_entity_id": supplier_id,
        "supplier_name": entities[supplier_id]["canonical_name"],
        "customer_entity_id": customer_id,
        "customer_name": entities[customer_id]["canonical_name"],
        # Generic endpoints are authoritative for non-supply families. The
        # legacy supplier/customer aliases remain for backward compatibility.
        "source_entity_id": supplier_id,
        "source_entity_name": entities[supplier_id]["canonical_name"],
        "target_entity_id": customer_id,
        "target_entity_name": entities[customer_id]["canonical_name"],
        "source_role": source_role,
        "target_role": target_role,
        "relationship_type": relationship_type,
        "relationship_family": relationship_family,
        "product_or_service": product_or_service,
        "categories": sorted({category_for_raw(row.relation_type) for row in rows if category_for_raw(row.relation_type)}),
        "source_relation_types": sorted({row.relation_type for row in rows}),
        "modality": modality,
        "confidence": round(sum(row.confidence_score for row in rows) / len(rows), 3),
        "evidence_count": len(rows),
        "evidence_ids": evidence_ids,
        "issuer_names": sorted({row.subject for row in rows}),
        "source_accession_numbers": sorted({row.accession_number for row in rows if row.accession_number}),
        "source_types": sorted({row.form for row in rows}),
        "first_observed_date": dates[0] if dates else "",
        "last_observed_date": dates[-1] if dates else "",
        "resolution_status": "canonical",
        "risk_flags": sorted({flag for row in rows for flag in row.risk_flags}),
    }


def selected_product_or_service(rows: list[RelationEvidence]) -> str:
    """Choose a stated product deterministically without promoting a guess."""
    values = [row.product_or_service.strip() for row in rows if row.product_or_service.strip()]
    if not values:
        return ""
    counts = defaultdict(int)
    display: dict[str, str] = {}
    for value in values:
        key = value.casefold()
        counts[key] += 1
        display.setdefault(key, value)
    return display[sorted(counts, key=lambda key: (-counts[key], key))[0]]


def endpoint_roles(family: str, relationship_type: str) -> tuple[str, str]:
    if family == "supply_chain":
        return ("supplier", "customer")
    if family == "ownership_control":
        return ("controller", "controlled_entity")
    if family == "corporate_transaction":
        return ({"acquires_asset_from": "acquirer", "acquires_company": "acquirer", "invests_in": "investor"}.get(relationship_type, "source"), "target")
    if relationship_type == "licenses_to":
        return ("licensor", "licensee")
    if relationship_type == "partners_with":
        return ("partner", "partner")
    if relationship_type == "co_invests_with":
        return ("co_investor", "co_investor")
    return ("source", "target")


def relationship_review_queue(relationships: list[dict[str, object]]) -> list[dict[str, object]]:
    """Transparent priority queue; unreviewed candidates are not discarded."""
    rows: list[dict[str, object]] = []
    for row in relationships:
        if row.get("review_status") not in {"unreviewed", "needs_review"}:
            continue
        score = int(row.get("evidence_count", 0)) * 3 + len(row.get("source_accession_numbers", [])) * 4
        if row.get("relationship_family") == "supply_chain":
            score += 8
        if row.get("modality") == "current_fact":
            score += 3
        rows.append({
            "relationship_id": row.get("relationship_id"), "source": row.get("source_entity_name", row.get("supplier_name")),
            "target": row.get("target_entity_name", row.get("customer_name")), "source_role": row.get("source_role", "supplier"),
            "target_role": row.get("target_role", "customer"), "relationship_family": row.get("relationship_family"),
            "relationship_type": row.get("relationship_type"), "evidence_count": row.get("evidence_count"),
            "filing_count": len(row.get("source_accession_numbers", [])), "priority_score": score,
            "reason": "Current candidate with direct evidence; ranked by evidence, filing support, supply-chain relevance and modality.",
        })
    return sorted(rows, key=lambda item: (-int(item["priority_score"]), str(item["source"])))


def apply_cross_filing_verification(relationships: list[dict[str, object]]) -> None:
    """Accept a fact only when two distinct SEC documents strongly support it.

    The reporting issuer may be the same (for example, a 10-K and 10-Q); the
    independence requirement is at the filing/document level, not at the
    company level. This rule never invents a new fact: it only promotes an
    already canonical relation with matching endpoints, type and product.
    """
    groups: dict[tuple[str, str, str, str, str], list[dict[str, object]]] = defaultdict(list)
    for relationship in relationships:
        groups[(
            str(relationship["supplier_entity_id"]), str(relationship["customer_entity_id"]),
            str(relationship["relationship_family"]), str(relationship["relationship_type"]),
            str(relationship.get("product_or_service", "")).casefold(),
        )].append(relationship)
    for group in groups.values():
        accessions = {accession for row in group for accession in row.get("source_accession_numbers", [])}
        strong = all(float(row.get("confidence", 0)) >= 0.75 for row in group)
        blocking_risks = blocking_risk_flags()
        has_blocking_risk = any(blocking_risks.intersection(set(row.get("risk_flags", []))) for row in group)
        if len(accessions) >= 2 and strong and not has_blocking_risk:
            for row in group:
                row["verification_status"] = "cross_filing_verified"
                row["review_status"] = "accepted"
                row["decision"] = "accept"
                row["decision_source"] = "cross_filing_rule"
                row["decision_reason"] = f"Automatically accepted: {len(accessions)} independent SEC filings support the same relationship."
        else:
            for row in group:
                row["verification_status"] = "single_filing_candidate"
                row["review_status"] = "unreviewed"
                row["decision"] = "review"
                row["decision_source"] = "pending_review"


def is_non_entity_fragment(name: str) -> bool:
    """Reject table headings and orphaned legal suffix fragments as graph nodes."""
    if re.search(r"\b(?:inc\.?|ltd\.?|llc|plc|corp\.?|corporation|company)\b", name, flags=re.IGNORECASE):
        return False
    key = object_key(name)
    if key in NON_ENTITY_BLOCKLIST:
        return True
    return key.endswith(" subsidiaries") or key.endswith(" businesses")


def apply_parentage(entities: dict[str, dict[str, object]], relationships: list[dict[str, object]]) -> None:
    """Annotate direct parentage for UI collapse/expand without erasing legal entities."""
    parent_by_child: dict[str, str] = {}
    for row in relationships:
        if row.get("relationship_family") != "ownership_control":
            continue
        parent_by_child.setdefault(str(row["customer_entity_id"]), str(row["supplier_entity_id"]))
    for child_id, parent_id in parent_by_child.items():
        if child_id not in entities or parent_id not in entities or child_id == parent_id:
            continue
        entities[child_id]["parent_entity_id"] = parent_id
        entities[child_id]["parent_name"] = entities[parent_id]["canonical_name"]


def diagnostic(record: RelationEvidence, status: str, normalized_object: str) -> dict[str, object]:
    return {
        "passage_id": record.passage_id,
        "subject": record.subject,
        "raw_object": record.object,
        "normalized_object": normalized_object,
        "relation_type": record.relation_type,
        "status": status,
        "source_document_url": record.source_document_url,
    }
