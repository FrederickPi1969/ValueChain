"""Persistable entity mentions, alias clusters, and narrow issuer coreference.

This is deliberately a pre-canonical layer: it records what appeared in a
passage before a relationship extractor decides whether it forms a fact.
"""

from __future__ import annotations

import re
from collections import defaultdict
from hashlib import sha256

from valuechain.edge_quality import object_key
from valuechain.entity_resolution import EntityResolver, clean_organization_name, looks_like_organization_name, split_counterparty_list
from valuechain.models import Company, EntityMention, MentionCluster, Passage


GEOGRAPHY_NAMES = {
    "china", "hong kong", "macau", "taiwan", "japan", "russia", "singapore", "australia", "europe",
    "united states", "united kingdom", "canada", "france", "germany", "israel", "india", "korea", "south korea", "malaysia",
    "the eu", "the u s", "united arab emirates",
}
PRODUCT_OR_TECHNOLOGY_NAMES = {
    "apu", "apus", "cpu", "cpus", "gpu", "gpus", "fpga", "fpgas", "dpu", "dpus", "ssd", "ssds",
    "dram", "hbm", "nand", "managed nand", "csp", "csps", "oem", "oems", "odm", "odms",
}
ROLE_MARKERS = ("chief executive officer", "chief financial officer", "board of directors", "our chief")


ISSUER_REFERENCE_RE = re.compile(r"\b(?:we|our|us|the company|the registrant)\b", re.IGNORECASE)


def extract_passage_mentions(passages: list[Passage], companies: list[Company]) -> list[EntityMention]:
    """Extract every resolved name occurrence plus safe issuer self-references.

    We intentionally do not resolve vague phrases (for example "our foundry")
    as entities.  Only explicit names and issuer-bound first-person references
    are emitted.
    """
    resolver = EntityResolver(companies)
    rows: list[EntityMention] = []
    for passage in passages:
        rows.extend(_named_mentions_for_passage(passage, resolver))
        rows.extend(_issuer_references_for_passage(passage))
    return sorted(rows, key=lambda row: (row.passage_id, row.start_offset, row.end_offset, row.normalized_name))


def build_mention_clusters(mentions: list[EntityMention]) -> list[MentionCluster]:
    grouped: dict[str, list[EntityMention]] = defaultdict(list)
    for mention in mentions:
        # Issuer references join the issuer's entity cluster; unresolved named
        # entities remain a separate proposed-name cluster rather than merging
        # by fuzzy similarity.
        key = object_key(mention.normalized_name or mention.text)
        if key:
            grouped[key].append(mention)
    clusters: list[MentionCluster] = []
    for key, rows in grouped.items():
        first = rows[0]
        canonical_name = next((row.normalized_name for row in rows if row.normalized_name), first.text)
        canonical_entity_id = f"entity:{object_key(canonical_name)}" if first.resolution_status != "unresolved" else ""
        cluster_id = f"mention-cluster:{sha256(key.encode()).hexdigest()[:16]}"
        for row in rows:
            row.cluster_id = cluster_id
            row.canonical_entity_id = canonical_entity_id
        clusters.append(MentionCluster(
            cluster_id=cluster_id,
            normalized_key=key,
            representative_name=first.text,
            proposed_canonical_name=canonical_name,
            canonical_entity_id=canonical_entity_id,
            entity_type=first.entity_type,
            resolution_status=first.resolution_status,
            resolver_method=first.resolver_method,
            mention_count=len(rows),
        ))
    return sorted(clusters, key=lambda row: (row.proposed_canonical_name, row.cluster_id))


def build_alias_review_queue(
    mentions: list[EntityMention], clusters: list[MentionCluster], passages_by_id: dict[str, Passage]
) -> list[dict[str, object]]:
    """Surface unresolved organization-like names without creating graph nodes."""
    rows: list[dict[str, object]] = []
    mentions_by_cluster: dict[str, list[EntityMention]] = defaultdict(list)
    for mention in mentions:
        mentions_by_cluster[mention.cluster_id].append(mention)
    for triage in build_entity_triage(mentions, clusters, passages_by_id):
        if triage["disposition"] != "review_organization_candidate":
            continue
        rows.append({
            **triage,
            "suggested_action": "Resolve to an existing canonical entity, create a new entity, or mark non-entity.",
            "review_status": "unreviewed",
        })
    return sorted(rows, key=lambda row: (-int(row["mention_count"]), str(row["proposed_name"])))


def build_entity_triage(
    mentions: list[EntityMention], clusters: list[MentionCluster], passages_by_id: dict[str, Passage]
) -> list[dict[str, object]]:
    """Classify unresolved clusters before any external company resolution."""
    mentions_by_cluster: dict[str, list[EntityMention]] = defaultdict(list)
    for mention in mentions:
        mentions_by_cluster[mention.cluster_id].append(mention)
    rows: list[dict[str, object]] = []
    for cluster in clusters:
        members = mentions_by_cluster.get(cluster.cluster_id, [])
        if not members:
            continue
        sample = members[0]
        passage = passages_by_id.get(sample.passage_id)
        entity_class, disposition = classify_cluster(cluster)
        rows.append({
            "cluster_id": cluster.cluster_id,
            "proposed_name": cluster.proposed_canonical_name,
            "entity_type": cluster.entity_type,
            "entity_class": entity_class,
            "disposition": disposition,
            "mention_count": cluster.mention_count,
            "sample_text": sample.text,
            "sample_passage_id": sample.passage_id,
            "sample_accession_number": passage.accession_number if passage else "",
            "sample_source_url": passage.source_document_url if passage else "",
            "sample_context": passage.text[:500] if passage else "",
        })
    return sorted(rows, key=lambda row: (-int(row["mention_count"]), str(row["proposed_name"])))


def classify_cluster(cluster: MentionCluster) -> tuple[str, str]:
    """Conservative routing, not a claim that an unresolved name is a company."""
    key = object_key(cluster.proposed_canonical_name)
    if key in GEOGRAPHY_NAMES:
        return ("geography", "retain_non_company")
    if key in PRODUCT_OR_TECHNOLOGY_NAMES:
        return ("product_or_technology", "retain_non_company")
    if any(marker in key for marker in ROLE_MARKERS):
        return ("person_or_title", "exclude_non_entity")
    if key in {"domestic subsidiaries", "foreign subsidiaries", "businesses", "networks", "engineering", "service"}:
        return ("non_entity_fragment", "exclude_non_entity")
    if cluster.resolution_status in {"universe_alias", "issuer_resolved"}:
        return ("resolved_company", "already_resolved")
    if cluster.resolution_status == "name_resolved":
        return ("organization_candidate", "review_organization_candidate")
    return ("organization_candidate", "review_organization_candidate")


def _named_mentions_for_passage(passage: Passage, resolver: EntityResolver) -> list[EntityMention]:
    rows: list[EntityMention] = []
    occupied: set[tuple[int, int]] = set()
    # Exhibit 21 is parsed as `legal entity | jurisdiction`.  Preserve its
    # first cell as a mention even when its jurisdictional suffix is uncommon
    # (GmbH, S.A.S., AB, etc.).  Canonicalization later blocklists headings.
    if "|" in passage.text:
        first_cell = passage.text.split("|", 1)[0].strip()
        if first_cell and looks_like_organization_name(first_cell):
            start = passage.text.find(first_cell)
            span = (start, start + len(first_cell))
            occupied.add(span)
            rows.append(_mention(passage, first_cell, span, "organization", first_cell, "", "", 0.74, "name_resolved", "table_row_ner"))
    for alias, company in sorted(resolver.alias_to_company.items(), key=lambda item: len(item[0]), reverse=True):
        flags = 0 if alias in resolver.uppercase_only_aliases and len(alias) <= 5 else re.IGNORECASE
        needle = alias.upper() if flags == 0 else alias
        for match in re.finditer(rf"\b{re.escape(needle)}\b", passage.text, flags=flags):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            occupied.add(span)
            rows.append(_mention(
                passage, match.group(0), span, "company", company.company_name,
                company.ticker, company.cik, 0.85, "universe_alias", "universe_alias",
            ))
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,8}\s*,?\s+(?:Inc\.?|Incorporated|Corporation|Corp\.?|Company|Co\.?|Ltd\.?|Limited|plc|PLC|N\.V\.|S\.A\.|LLC|Holdings)(?:,\s*Ltd\.?)?)\b", passage.text):
        name = clean_organization_name(match.group(1))
        if looks_like_organization_name(name) and not any(match.start(1) < end and start < match.end(1) for start, end in occupied):
            occupied.add(match.span(1))
            rows.append(_mention(passage, name, match.span(1), "organization", name, "", "", 0.68, "name_resolved", "legal_suffix_ner"))
    # Lists often contain names such as "SK Hynix" without a legal suffix.
    # They remain named mentions, not automatically canonical companies.
    for list_match in re.finditer(r"\b(?:such as|including|include|includes|from)\s+([^;\n]{1,260})", passage.text, re.IGNORECASE):
        segment, cursor = list_match.group(1), list_match.start(1)
        for item in split_counterparty_list(segment):
            if not looks_like_organization_name(item):
                continue
            found = passage.text.find(item, cursor, list_match.end(1))
            if found < 0:
                continue
            cursor = found + len(item)
            span = (found, found + len(item))
            if not any(span[0] < end and start < span[1] for start, end in occupied):
                occupied.add(span)
                rows.append(_mention(passage, item, span, "organization", item, "", "", 0.58, "unresolved", "list_ner"))
    return _dedupe_by_span(rows)


def _issuer_references_for_passage(passage: Passage) -> list[EntityMention]:
    return [
        _mention(passage, match.group(0), match.span(), "company", passage.company_name,
                 passage.ticker, passage.cik, 0.99, "issuer_resolved", "issuer_context", "issuer_reference")
        for match in ISSUER_REFERENCE_RE.finditer(passage.text)
    ]


def _mention(passage: Passage, text: str, span: tuple[int, int], entity_type: str, normalized_name: str,
             ticker: str, cik: str, confidence: float, status: str, method: str,
             kind: str = "named_entity") -> EntityMention:
    fingerprint = f"{passage.passage_id}|{span[0]}|{span[1]}|{object_key(normalized_name)}"
    return EntityMention(
        text=text, entity_type=entity_type, normalized_name=normalized_name, ticker=ticker, cik=cik,
        confidence=confidence, mention_id=f"mention:{sha256(fingerprint.encode()).hexdigest()[:20]}",
        passage_id=passage.passage_id, start_offset=span[0], end_offset=span[1], mention_kind=kind,
        resolution_status=status, resolver_method=method,
    )


def _dedupe_by_span(rows: list[EntityMention]) -> list[EntityMention]:
    seen: set[tuple[int, int, str]] = set()
    result: list[EntityMention] = []
    for row in rows:
        key = (row.start_offset, row.end_offset, row.normalized_name.casefold())
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result
