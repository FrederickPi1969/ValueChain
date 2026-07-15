from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import replace
from typing import Iterable

from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.multilingual.types import LanguagePack, ParsedDocument


LATIN_NUMBER_RE = re.compile(r"[a-z][a-z0-9_.%+-]*|\d+(?:[.,]\d+)*%?", re.IGNORECASE)
CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")
HANGUL_RUN_RE = re.compile(r"[\uac00-\ud7a3]+")


def tokenize(text: str, language: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = LATIN_NUMBER_RE.findall(normalized)
    if language in {"zh-Hans", "zh-Hant", "ja"}:
        for run in CJK_RUN_RE.findall(normalized):
            tokens.extend(_character_ngrams(run))
    elif language == "ko":
        for run in HANGUL_RUN_RE.findall(normalized):
            tokens.append(run)
            tokens.extend(_character_ngrams(run))
    return tokens


def _character_ngrams(value: str) -> list[str]:
    if len(value) < 2:
        return [value]
    return [value[index : index + 2] for index in range(len(value) - 1)]


class MultilingualBM25:
    def __init__(
        self,
        chunks: list[DocumentChunk],
        language: str,
        *,
        k1: float = 1.4,
        b: float = 0.72,
    ) -> None:
        self.chunks = chunks
        self.language = language
        self.k1 = k1
        self.b = b
        self.tokens = [tokenize(chunk.text, language) for chunk in chunks]
        self.term_counts = [Counter(row) for row in self.tokens]
        self.average_length = sum(map(len, self.tokens)) / max(1, len(self.tokens))
        document_frequency: Counter[str] = Counter()
        for row in self.tokens:
            document_frequency.update(set(row))
        count = max(1, len(chunks))
        self.idf = {
            term: math.log(1 + (count - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def search(self, query: str, *, limit: int = 10) -> list[DocumentChunk]:
        terms = list(dict.fromkeys(tokenize(query, self.language)))
        ranked: list[tuple[float, int]] = []
        for index, counts in enumerate(self.term_counts):
            length = len(self.tokens[index])
            score = 0.0
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * length / max(1.0, self.average_length)
                )
                score += self.idf.get(term, 0.0) * frequency * (self.k1 + 1) / denominator
            if score > 0:
                ranked.append((score, index))
        ranked.sort(reverse=True)
        return [
            replace(self.chunks[index], score=round(score, 6))
            for score, index in ranked[:limit]
        ]


def select_profile_chunks(
    parsed: ParsedDocument,
    pack: LanguagePack,
    *,
    limit: int = 10,
) -> list[DocumentChunk]:
    chunks = parsed.chunks
    if not chunks:
        return []
    if parsed.source.document_granularity == "event_disclosure":
        return chunks[: min(limit, len(chunks))]
    index = MultilingualBM25(chunks, pack.code)
    scores: Counter[str] = Counter()
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    anchors = [chunk for chunk in chunks if chunk.section_hint in {"business", "mdna"}][:3]
    for query in pack.profile_queries:
        for rank, chunk in enumerate(index.search(query, limit=10), start=1):
            scores[chunk.chunk_id] += 1 / (20 + rank)
    for anchor in anchors:
        scores[anchor.chunk_id] += 0.2
    if not scores:
        return chunks[:limit]
    return _include_anchors(
        [by_id[chunk_id] for chunk_id, _ in scores.most_common() if chunk_id in by_id],
        anchors,
        limit,
    )


def select_signal_chunks(
    parsed: ParsedDocument,
    pack: LanguagePack,
    *,
    limit: int = 18,
) -> list[DocumentChunk]:
    chunks = parsed.chunks
    if not chunks:
        return []
    if parsed.source.document_granularity == "event_disclosure":
        return chunks[: min(limit, len(chunks))]
    index = MultilingualBM25(chunks, pack.code)
    scores: Counter[str] = Counter()
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    anchors: list[DocumentChunk] = []
    for _, query in pack.signal_queries:
        matches = index.search(query, limit=7)
        if matches:
            anchors.append(matches[0])
        for rank, chunk in enumerate(matches, start=1):
            scores[chunk.chunk_id] += 1 / (25 + rank)
    for chunk in chunks:
        if chunk.section_hint in {"risk", "supply_chain", "research"}:
            scores[chunk.chunk_id] += 0.08
    if not scores:
        return chunks[:limit]
    ranked = [by_id[chunk_id] for chunk_id, _ in scores.most_common() if chunk_id in by_id]
    return _include_anchors(ranked, anchors, limit)


def _include_anchors(
    ranked: list[DocumentChunk],
    anchors: list[DocumentChunk],
    limit: int,
) -> list[DocumentChunk]:
    required = _deduplicate(anchors)[:limit]
    selected = _deduplicate(ranked)[:limit]
    selected_ids = {chunk.chunk_id for chunk in selected}
    required_ids = {chunk.chunk_id for chunk in required}
    for anchor in required:
        if anchor.chunk_id in selected_ids:
            continue
        replacement = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if selected[index].chunk_id not in required_ids
            ),
            None,
        )
        if replacement is None:
            if len(selected) < limit:
                selected.append(anchor)
        else:
            selected_ids.discard(selected[replacement].chunk_id)
            selected[replacement] = anchor
        selected_ids.add(anchor.chunk_id)
    return selected


def _deduplicate(chunks: Iterable[DocumentChunk]) -> list[DocumentChunk]:
    unique: dict[str, DocumentChunk] = {}
    for chunk in chunks:
        unique.setdefault(chunk.chunk_id, chunk)
    return list(unique.values())
