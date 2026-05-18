from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable

import httpx

from valuechain.edge_quality import canonical_company_name, is_placeholder_object, object_key
from valuechain.models import RelationEvidence


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    proxy_url: str = ""
    timeout_s: int = 120
    batch_size: int = 64


class OpenAIEmbeddingClient:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), max(1, self.config.batch_size)):
            vectors.extend(self._embed_batch(texts[start : start + self.config.batch_size]))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self.config.model, "input": texts}
        request_kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {self.config.api_key}"},
            "json": payload,
            "timeout": self.config.timeout_s,
        }
        if self.config.proxy_url:
            request_kwargs["proxy"] = self.config.proxy_url
        response = httpx.post(f"{self.config.base_url.rstrip('/')}/embeddings", **request_kwargs)
        response.raise_for_status()
        rows = response.json().get("data", [])
        ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
        return [list(map(float, row["embedding"])) for row in ordered]


def embedding_merge_relation_evidence(
    records: list[RelationEvidence],
    client: OpenAIEmbeddingClient,
    threshold: float = 0.92,
) -> tuple[list[RelationEvidence], list[dict[str, object]]]:
    labels = candidate_object_labels(records)
    if len(labels) < 2:
        return records, []
    vectors = client.embed_texts(labels)
    diagnostics: list[dict[str, object]] = []
    alias_map = build_embedding_alias_map(labels, vectors, threshold=threshold, diagnostics=diagnostics)
    if not alias_map:
        return records, diagnostics
    merged: list[RelationEvidence] = []
    for record in records:
        target = alias_map.get(record.object)
        if target and target != record.object:
            diagnostics.append(
                {
                    "action": "merge",
                    "original_object": record.object,
                    "merged_object": target,
                    "relation_type": record.relation_type,
                    "subject": record.subject,
                    "passage_id": record.passage_id,
                    "threshold": threshold,
                }
            )
            merged.append(replace(record, object=target))
        else:
            merged.append(record)
    return merged, diagnostics


def candidate_object_labels(records: Iterable[RelationEvidence]) -> list[str]:
    labels = {
        record.object.strip()
        for record in records
        if record.object.strip() and not is_placeholder_object(record.object)
    }
    return sorted(labels)


def build_embedding_alias_map(
    labels: list[str],
    vectors: list[list[float]],
    threshold: float = 0.92,
    diagnostics: list[dict[str, object]] | None = None,
) -> dict[str, str]:
    if len(labels) != len(vectors):
        raise ValueError("labels and vectors must have the same length")
    clusters = cluster_labels(labels, vectors, threshold=threshold, diagnostics=diagnostics)
    alias_map: dict[str, str] = {}
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        representative = choose_cluster_representative(cluster)
        for label in cluster:
            if label != representative:
                alias_map[label] = representative
    return alias_map


def cluster_labels(
    labels: list[str],
    vectors: list[list[float]],
    threshold: float = 0.92,
    diagnostics: list[dict[str, object]] | None = None,
    exact_pair_limit: int = 1000,
    lsh_bits: int = 80,
    band_size: int = 10,
    max_bucket_size: int = 256,
) -> list[list[str]]:
    parent = {label: label for label in labels}

    def find(label: str) -> str:
        while parent[label] != label:
            parent[label] = parent[parent[label]]
            label = parent[label]
        return label

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    pairs, index_stats = ann_candidate_pairs(
        labels,
        vectors,
        exact_pair_limit=exact_pair_limit,
        lsh_bits=lsh_bits,
        band_size=band_size,
        max_bucket_size=max_bucket_size,
    )
    compared_pairs = 0
    merged_pairs = 0
    for i, j in pairs:
        compared_pairs += 1
        similarity = cosine_similarity(vectors[i], vectors[j])
        if similarity >= threshold and should_allow_embedding_merge(labels[i], labels[j], similarity):
            union(labels[i], labels[j])
            merged_pairs += 1
    if diagnostics is not None:
        diagnostics.append(
            {
                "action": "ann_index",
                "label_count": len(labels),
                "candidate_pairs": len(pairs),
                "compared_pairs": compared_pairs,
                "merged_pairs": merged_pairs,
                "threshold": threshold,
                **index_stats,
            }
        )
    clusters_by_root: dict[str, list[str]] = {}
    for label in labels:
        clusters_by_root.setdefault(find(label), []).append(label)
    return [sorted(cluster) for cluster in clusters_by_root.values()]


def choose_cluster_representative(cluster: list[str]) -> str:
    return sorted(cluster, key=representative_sort_key)[0]


def representative_sort_key(label: str) -> tuple[int, int, int, int, str]:
    key = object_key(label)
    words = key.split()
    has_known_company = bool(canonical_company_name(label))
    bad_prefix = bool(words and words[0] in FRAGMENT_PREFIXES)
    has_legal_suffix = bool(re.search(r"\b(inc|corporation|corp|company|limited|ltd|llc|plc|n\.v\.|s\.a\.)\b", label, flags=re.IGNORECASE))
    return (
        0 if has_known_company else 1,
        1 if bad_prefix else 0,
        0 if has_legal_suffix else 1,
        abs(len(words) - 2),
        label.lower(),
    )


def should_allow_embedding_merge(left: str, right: str, similarity: float = 0.0) -> bool:
    left_surface_key = surface_merge_key(left)
    right_surface_key = surface_merge_key(right)
    if not left_surface_key or not right_surface_key:
        return False
    if left_surface_key == right_surface_key:
        return True

    left_key = suffixless_merge_key(left)
    right_key = suffixless_merge_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key and legal_suffixes_compatible(left, right):
        return True

    left_canonical = canonical_company_name(left)
    right_canonical = canonical_company_name(right)
    if left_canonical and right_canonical:
        return object_key(left_canonical) == object_key(right_canonical)

    left_tokens = material_merge_tokens(left)
    right_tokens = material_merge_tokens(right)
    if is_acronym_alias(left_tokens, right_key) or is_acronym_alias(right_tokens, left_key):
        return True

    return False


def cleaned_merge_key(label: str) -> str:
    return suffixless_merge_key(label)


def suffixless_merge_key(label: str) -> str:
    tokens = normalized_merge_tokens(label)
    while tokens and tokens[-1] in LEGAL_SUFFIX_TOKEN_MAP:
        tokens.pop()
    return " ".join(tokens)


def surface_merge_key(label: str) -> str:
    return " ".join(normalized_merge_tokens(label))


def normalized_merge_tokens(label: str) -> list[str]:
    text = label.strip()
    text = re.sub(r"\(\s*\d+\s*\)?\s*$", "", text)
    text = re.sub(r"\s+\d+\s*$", "", text)
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().split()


def legal_suffixes_compatible(left: str, right: str) -> bool:
    return legal_suffix_signature(left) == legal_suffix_signature(right)


def legal_suffix_signature(label: str) -> tuple[str, ...]:
    tokens = normalized_merge_tokens(label)
    suffixes: list[str] = []
    while tokens and tokens[-1] in LEGAL_SUFFIX_TOKEN_MAP:
        suffixes.append(LEGAL_SUFFIX_TOKEN_MAP[tokens.pop()])
    return tuple(reversed(suffixes))


def material_merge_tokens(label: str) -> tuple[str, ...]:
    return tuple(token for token in cleaned_merge_key(label).split() if token not in MERGE_LEGAL_TOKENS)


def is_acronym_alias(tokens: tuple[str, ...], candidate_key: str) -> bool:
    if len(candidate_key) < 3 or " " in candidate_key or not tokens:
        return False
    acronym = "".join(token[0] for token in tokens if token)
    return acronym == candidate_key


FRAGMENT_PREFIXES = {
    "contents",
    "notes",
    "item",
    "part",
    "table",
    "business",
    "risk",
    "management",
    "overview",
}


MERGE_LEGAL_TOKENS = {
    "inc",
    "incorporated",
    "corporation",
    "corp",
    "company",
    "co",
    "limited",
    "ltd",
    "llc",
    "plc",
    "holdings",
    "holding",
    "group",
    "n",
    "v",
    "s",
    "a",
    "nv",
    "sa",
    "ag",
    "bv",
    "gmbh",
    "pte",
}


LEGAL_SUFFIX_TOKEN_MAP = {
    "inc": "inc",
    "incorporated": "inc",
    "corporation": "corporation",
    "corp": "corporation",
    "company": "company",
    "co": "company",
    "limited": "limited",
    "ltd": "limited",
    "llc": "llc",
    "plc": "plc",
    "holdings": "holdings",
    "holding": "holding",
    "group": "group",
    "nv": "nv",
    "sa": "sa",
    "ag": "ag",
    "bv": "bv",
    "gmbh": "gmbh",
    "pte": "pte",
    "lp": "lp",
    "llp": "llp",
}


def ann_candidate_pairs(
    labels: list[str],
    vectors: list[list[float]],
    exact_pair_limit: int = 1000,
    lsh_bits: int = 80,
    band_size: int = 10,
    max_bucket_size: int = 256,
) -> tuple[list[tuple[int, int]], dict[str, object]]:
    if len(labels) != len(vectors):
        raise ValueError("labels and vectors must have the same length")
    if len(labels) <= exact_pair_limit:
        return list(itertools.combinations(range(len(labels)), 2)), {
            "ann_mode": "exact_small",
            "lsh_bits": 0,
            "band_size": 0,
            "blocked_pairs": 0,
            "lsh_pairs": 0,
            "skipped_large_buckets": 0,
        }

    blocked_pairs = blocking_candidate_pairs(labels, max_bucket_size=max_bucket_size)
    lsh_pairs, lsh_stats = lsh_candidate_pairs(
        vectors,
        bits=lsh_bits,
        band_size=band_size,
        max_bucket_size=max_bucket_size,
    )
    pairs = sorted(blocked_pairs | lsh_pairs)
    return pairs, {
        "ann_mode": "blocked_lsh",
        "lsh_bits": lsh_bits,
        "band_size": band_size,
        "blocked_pairs": len(blocked_pairs),
        "lsh_pairs": len(lsh_pairs),
        **lsh_stats,
    }


def blocking_candidate_pairs(labels: list[str], max_bucket_size: int = 256) -> set[tuple[int, int]]:
    buckets: dict[str, list[int]] = {}
    for idx, label in enumerate(labels):
        for key in blocking_keys(label):
            buckets.setdefault(key, []).append(idx)
    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > max_bucket_size:
            continue
        for left, right in itertools.combinations(sorted(set(members)), 2):
            pairs.add((left, right))
    return pairs


def blocking_keys(label: str) -> set[str]:
    key = object_key(label)
    if not key:
        return set()
    tokens = [token for token in key.split() if token not in ENTITY_STOPWORDS]
    keys = {f"key:{key}"}
    if tokens:
        keys.add(f"head:{tokens[0]}")
        keys.add(f"tail:{tokens[-1]}")
    if len(tokens) >= 2:
        keys.add(f"head2:{tokens[0]} {tokens[1]}")
        keys.add(f"tail2:{tokens[-2]} {tokens[-1]}")
    acronym = "".join(token[0] for token in tokens if token)
    if len(acronym) >= 2:
        keys.add(f"acro:{acronym}")
    for token in tokens:
        if len(token) >= 5:
            keys.add(f"tok:{token}")
    return keys


ENTITY_STOPWORDS = {
    "inc",
    "incorporated",
    "corporation",
    "corp",
    "company",
    "co",
    "limited",
    "ltd",
    "llc",
    "plc",
    "holdings",
    "holding",
    "the",
    "and",
    "of",
}


def lsh_candidate_pairs(
    vectors: list[list[float]],
    bits: int = 80,
    band_size: int = 10,
    max_bucket_size: int = 256,
) -> tuple[set[tuple[int, int]], dict[str, object]]:
    signatures = simhash_signatures(vectors, bits=bits)
    pairs: set[tuple[int, int]] = set()
    skipped_large_buckets = 0
    total_buckets = 0
    for start in range(0, bits, band_size):
        buckets: dict[str, list[int]] = {}
        end = min(start + band_size, bits)
        for idx, signature in enumerate(signatures):
            buckets.setdefault(signature[start:end], []).append(idx)
        for members in buckets.values():
            total_buckets += 1
            unique_members = sorted(set(members))
            if len(unique_members) < 2:
                continue
            if len(unique_members) > max_bucket_size:
                skipped_large_buckets += 1
                continue
            for left, right in itertools.combinations(unique_members, 2):
                pairs.add((left, right))
    return pairs, {
        "lsh_bucket_count": total_buckets,
        "skipped_large_buckets": skipped_large_buckets,
    }


def simhash_signatures(vectors: list[list[float]], bits: int = 64) -> list[str]:
    try:
        import numpy as np
    except ImportError:
        return lexical_signatures(vectors, bits=bits)
    if not vectors:
        return []
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError("vectors must be two-dimensional")
    rng = np.random.default_rng(1969)
    hyperplanes = rng.standard_normal((matrix.shape[1], bits), dtype=np.float32)
    signs = matrix @ hyperplanes >= 0
    return ["".join("1" if value else "0" for value in row) for row in signs]


def lexical_signatures(vectors: list[list[float]], bits: int = 64) -> list[str]:
    signatures: list[str] = []
    for vector in vectors:
        chunks = []
        for idx in range(bits):
            value = vector[idx % len(vector)] if vector else 0.0
            chunks.append("1" if value >= 0 else "0")
        signatures.append("".join(chunks))
    return signatures


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have the same length")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
