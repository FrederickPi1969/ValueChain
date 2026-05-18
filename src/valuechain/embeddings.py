from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Iterable

import httpx

from valuechain.edge_quality import is_placeholder_object
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
    alias_map = build_embedding_alias_map(labels, vectors, threshold=threshold)
    if not alias_map:
        return records, []
    merged: list[RelationEvidence] = []
    diagnostics: list[dict[str, object]] = []
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
) -> dict[str, str]:
    if len(labels) != len(vectors):
        raise ValueError("labels and vectors must have the same length")
    clusters = cluster_labels(labels, vectors, threshold=threshold)
    alias_map: dict[str, str] = {}
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        representative = choose_cluster_representative(cluster)
        for label in cluster:
            if label != representative:
                alias_map[label] = representative
    return alias_map


def cluster_labels(labels: list[str], vectors: list[list[float]], threshold: float = 0.92) -> list[list[str]]:
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

    for i, left in enumerate(labels):
        for j in range(i + 1, len(labels)):
            if cosine_similarity(vectors[i], vectors[j]) >= threshold:
                union(left, labels[j])
    clusters_by_root: dict[str, list[str]] = {}
    for label in labels:
        clusters_by_root.setdefault(find(label), []).append(label)
    return [sorted(cluster) for cluster in clusters_by_root.values()]


def choose_cluster_representative(cluster: list[str]) -> str:
    return sorted(cluster, key=lambda label: (-len(label), label.lower()))[0]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have the same length")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
