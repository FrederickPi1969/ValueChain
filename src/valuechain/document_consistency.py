"""Deterministic entity and relation consistency across filing passages."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import replace
from hashlib import sha256
from typing import Iterable

from valuechain.canonicalization import canonical_relation
from valuechain.edge_quality import object_key
from valuechain.models import EntityMention, MentionCluster, Passage, RelationEvidence
from valuechain.ontology import orientation_for_raw


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            parent = self.parent[item]
            self.parent[item] = root
            item = parent
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


class AliasAutomaton:
    """Small pure-Python Aho-Corasick automaton with deterministic outputs."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self.goto: list[dict[str, int]] = [{}]
        self.fail: list[int] = [0]
        self.output: list[list[tuple[str, str]]] = [[]]
        for alias, canonical_name in sorted(aliases.items()):
            state = 0
            for char in alias.casefold():
                if char not in self.goto[state]:
                    self.goto[state][char] = len(self.goto)
                    self.goto.append({})
                    self.fail.append(0)
                    self.output.append([])
                state = self.goto[state][char]
            self.output[state].append((alias, canonical_name))
        queue: deque[int] = deque(self.goto[0].values())
        while queue:
            state = queue.popleft()
            for char, target in self.goto[state].items():
                queue.append(target)
                fallback = self.fail[state]
                while fallback and char not in self.goto[fallback]:
                    fallback = self.fail[fallback]
                self.fail[target] = self.goto[fallback].get(char, 0)
                self.output[target].extend(self.output[self.fail[target]])

    def find(self, text: str) -> list[tuple[int, int, str, str]]:
        state = 0
        matches: list[tuple[int, int, str, str]] = []
        for index, char in enumerate(text.casefold()):
            while state and char not in self.goto[state]:
                state = self.fail[state]
            state = self.goto[state].get(char, 0)
            for alias, canonical_name in self.output[state]:
                start, end = index + 1 - len(alias), index + 1
                if _word_boundary(text, start, end):
                    matches.append((start, end, alias, canonical_name))
        return sorted(matches, key=lambda row: (row[0], -(row[1] - row[0]), row[2]))


def reconcile_document_mentions(
    passages: list[Passage],
    mentions: list[EntityMention],
) -> tuple[list[EntityMention], list[MentionCluster], list[dict[str, object]]]:
    passages_by_id = {passage.passage_id: passage for passage in passages}
    mentions_by_document: dict[tuple[str, str], list[EntityMention]] = defaultdict(list)
    passages_by_document: dict[tuple[str, str], list[Passage]] = defaultdict(list)
    for passage in passages:
        passages_by_document[_document_key(passage)].append(passage)
    for mention in mentions:
        passage = passages_by_id.get(mention.passage_id)
        if passage:
            mentions_by_document[_document_key(passage)].append(mention)

    diagnostics: list[dict[str, object]] = []
    augmented = list(mentions)
    for document_key, document_passages in passages_by_document.items():
        document_mentions = mentions_by_document.get(document_key, [])
        aliases = _document_aliases(document_passages, document_mentions)
        if not aliases:
            continue
        automaton = AliasAutomaton(aliases)
        for passage in document_passages:
            existing = [mention for mention in document_mentions if mention.passage_id == passage.passage_id]
            occupied = {(mention.start_offset, mention.end_offset) for mention in existing}
            for start, end, alias, canonical_name in automaton.find(passage.text):
                if any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied):
                    continue
                mention = _rescanned_mention(passage, start, end, canonical_name)
                augmented.append(mention)
                document_mentions.append(mention)
                occupied.add((start, end))
                diagnostics.append(
                    {
                        "action": "add_alias_mention",
                        "accession_number": passage.accession_number,
                        "passage_id": passage.passage_id,
                        "alias": alias,
                        "canonical_name": canonical_name,
                        "start_offset": start,
                        "end_offset": end,
                    }
                )
    augmented = _dedupe_mentions(augmented)
    clusters = _union_clusters(augmented)
    return augmented, clusters, diagnostics


def reconcile_document_relations(
    records: Iterable[RelationEvidence],
) -> tuple[list[RelationEvidence], list[dict[str, object]]]:
    kept: list[RelationEvidence] = []
    diagnostics: list[dict[str, object]] = []
    seen: set[tuple[str, ...]] = set()
    for record in records:
        evidence = re.sub(r"\s+", " ", record.evidence_quote or record.evidence_text).strip().casefold()
        key = (
            record.accession_number,
            object_key(record.subject),
            object_key(record.object),
            record.relation_type,
            record.modality,
            evidence,
        )
        if key in seen:
            diagnostics.append(
                {
                    "action": "drop",
                    "reason": "duplicate_across_passages",
                    "subject": record.subject,
                    "object": record.object,
                    "relation_type": record.relation_type,
                    "passage_id": record.passage_id,
                }
            )
            continue
        seen.add(key)
        kept.append(record)

    oriented: dict[tuple[str, str, str, str], dict[tuple[str, str], list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for index, record in enumerate(kept):
        relation_info = canonical_relation(record.relation_type)
        if not relation_info:
            continue
        _, canonical_type = relation_info
        subject, obj = object_key(record.subject), object_key(record.object)
        source, target = (
            (subject, obj)
            if orientation_for_raw(record.relation_type) == "subject_to_object"
            else (obj, subject)
        )
        group = (record.accession_number, *sorted((subject, obj)), canonical_type)
        oriented[group][(source, target)].append(index)
    for group, directions in oriented.items():
        if len(directions) < 2:
            continue
        affected = sorted({index for indexes in directions.values() for index in indexes})
        for index in affected:
            kept[index] = replace(
                kept[index],
                risk_flags=sorted(set(kept[index].risk_flags) | {"document_direction_conflict"}),
            )
        diagnostics.append(
            {
                "action": "flag",
                "reason": "document_direction_conflict",
                "accession_number": group[0],
                "entity_pair": " | ".join(group[1:3]),
                "canonical_relation": group[3],
                "evidence_count": len(affected),
            }
        )
    return kept, diagnostics


def _document_aliases(
    passages: list[Passage],
    mentions: list[EntityMention],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for mention in mentions:
        canonical = mention.normalized_name.strip()
        alias = mention.text.strip()
        is_trusted_named_alias = (
            mention.mention_kind != "issuer_reference"
            and mention.resolver_method in {"universe_alias", "legal_suffix_ner", "table_row_ner"}
        )
        if is_trusted_named_alias and canonical and alias and len(alias) >= 3:
            aliases.setdefault(alias, canonical)
    for passage in passages:
        passage_mentions = [mention for mention in mentions if mention.passage_id == passage.passage_id]
        for match in re.finditer(r"\((?:the\s+)?[\"“]?([A-Z][A-Z0-9.&-]{1,11})[\"”]?\)", passage.text):
            alias = match.group(1)
            antecedents = [mention for mention in passage_mentions if mention.end_offset <= match.start()]
            if not antecedents:
                continue
            antecedent = max(antecedents, key=lambda mention: mention.end_offset)
            if (
                match.start() - antecedent.end_offset <= 4
                and antecedent.normalized_name
                and antecedent.resolver_method in {"universe_alias", "legal_suffix_ner", "table_row_ner"}
            ):
                aliases.setdefault(alias, antecedent.normalized_name)
    return aliases


def _union_clusters(mentions: list[EntityMention]) -> list[MentionCluster]:
    union = UnionFind()
    by_key: dict[str, list[EntityMention]] = defaultdict(list)
    for mention in mentions:
        key = object_key(mention.normalized_name or mention.text)
        if not key:
            continue
        union.add(mention.mention_id)
        by_key[key].append(mention)
    for rows in by_key.values():
        for mention in rows[1:]:
            union.union(rows[0].mention_id, mention.mention_id)
    groups: dict[str, list[EntityMention]] = defaultdict(list)
    for mention in mentions:
        if mention.mention_id:
            groups[union.find(mention.mention_id)].append(mention)
    clusters: list[MentionCluster] = []
    for rows in groups.values():
        rows.sort(key=lambda row: (row.passage_id, row.start_offset, row.end_offset))
        canonical = next((row.normalized_name for row in rows if row.normalized_name), rows[0].text)
        resolved = next((row for row in rows if row.resolution_status != "unresolved"), rows[0])
        key = object_key(canonical)
        cluster_id = f"mention-cluster:{sha256(key.encode()).hexdigest()[:16]}"
        canonical_id = f"entity:{key}" if resolved.resolution_status != "unresolved" else ""
        for mention in rows:
            mention.cluster_id = cluster_id
            mention.canonical_entity_id = canonical_id
            mention.normalized_name = canonical
        clusters.append(
            MentionCluster(
                cluster_id=cluster_id,
                normalized_key=key,
                representative_name=rows[0].text,
                proposed_canonical_name=canonical,
                canonical_entity_id=canonical_id,
                entity_type=resolved.entity_type,
                resolution_status=resolved.resolution_status,
                resolver_method="document_alias_union",
                mention_count=len(rows),
            )
        )
    return sorted(clusters, key=lambda row: (row.proposed_canonical_name, row.cluster_id))


def _rescanned_mention(
    passage: Passage,
    start: int,
    end: int,
    canonical_name: str,
) -> EntityMention:
    fingerprint = f"{passage.passage_id}|{start}|{end}|{object_key(canonical_name)}"
    return EntityMention(
        text=passage.text[start:end],
        entity_type="company",
        normalized_name=canonical_name,
        confidence=0.8,
        mention_id=f"mention:{sha256(fingerprint.encode()).hexdigest()[:20]}",
        passage_id=passage.passage_id,
        start_offset=start,
        end_offset=end,
        mention_kind="alias_rescan",
        resolution_status="name_resolved",
        resolver_method="document_aho_corasick",
    )


def _dedupe_mentions(mentions: list[EntityMention]) -> list[EntityMention]:
    deduped: dict[tuple[str, int, int, str], EntityMention] = {}
    for mention in mentions:
        key = (
            mention.passage_id,
            mention.start_offset,
            mention.end_offset,
            object_key(mention.normalized_name or mention.text),
        )
        current = deduped.get(key)
        if current is None or mention.confidence > current.confidence:
            deduped[key] = mention
    return sorted(
        deduped.values(),
        key=lambda row: (row.passage_id, row.start_offset, row.end_offset, row.normalized_name),
    )


def _document_key(passage: Passage) -> tuple[str, str]:
    return (passage.accession_number, passage.source_document or passage.source_document_url)


def _word_boundary(text: str, start: int, end: int) -> bool:
    left_ok = start == 0 or not (text[start - 1].isalnum() or text[start - 1] == "_")
    right_ok = end == len(text) or not (text[end].isalnum() or text[end] == "_")
    return left_ok and right_ok
