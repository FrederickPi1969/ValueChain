from __future__ import annotations

import math
import re
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from valuechain.financial_ie.models import DocumentChunk


TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{1,}|\d+(?:\.\d+)?", re.IGNORECASE)
QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "capital expenditure": ("capex", "purchases of property plant and equipment", "pp&e"),
    "cost of goods sold": ("cogs", "cost of sales", "cost of revenue", "cost of products sold"),
    "accounts receivable": ("net receivables", "trade receivables", "receivables net"),
    "accounts payable": ("trade payables", "accounts payable and accrued liabilities"),
    "inventory": ("inventories", "merchandise inventories"),
    "total assets": ("consolidated balance sheets", "total assets"),
    "current assets": ("total current assets",),
    "current liabilities": ("total current liabilities",),
    "property plant and equipment": ("pp&e", "property equipment net", "fixed assets net"),
    "cash from operating activities": (
        "net cash provided by operating activities",
        "cash generated from operations",
        "operating cash flow",
    ),
    "depreciation and amortization": ("d&a", "depreciation amortization"),
    "net income": ("net earnings", "net loss", "net income attributable"),
    "free cash flow": ("operating cash flow", "capital expenditures", "cash provided by operating activities"),
    "revenue": ("net sales", "total revenues", "sales revenue"),
    "debt": ("borrowings", "notes payable", "long-term debt"),
    "research and development": ("r&d", "research development expense"),
    "stock based compensation": ("share-based compensation", "equity compensation"),
    "customer concentration": ("major customer", "significant customer", "accounted for"),
    "supplier concentration": ("sole source", "single source", "limited number of suppliers"),
    "operating income": ("income from operations", "operating profit"),
}


FINANCIAL_QUERY_FACETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"inventory turnover", ("cost of goods sold cost of sales cost of revenue", "inventory inventories")),
    (
        r"days payable|\bdpo\b",
        ("accounts payable trade payables", "cost of goods sold cost of sales", "inventory inventories"),
    ),
    (
        r"ebitda",
        ("operating income income from operations", "depreciation amortization", "revenue net sales"),
    ),
    (r"asset turnover", ("revenue net sales", "total assets")),
    (r"working capital", ("total current assets", "total current liabilities")),
    (r"return on assets|\broa\b", ("net income net loss", "total assets")),
    (r"\bcagr\b", ("revenue net sales total revenues",)),
    (r"capex|capital expenditure", ("capital expenditures purchases property plant equipment",)),
    (r"cost of goods sold|\bcogs\b", ("cost of goods sold cost of sales cost of revenue",)),
    (r"cash dividends|dividends.*paid", ("cash dividends dividends paid payments of dividends",)),
    (r"net (?:ar|accounts receivable)", ("accounts receivable net receivables",)),
    (r"net (?:ppne|pp&e)|net property,? plant", ("property plant equipment net pp&e",)),
    (r"cash flow from operating activities", ("net cash provided by operating activities",)),
    (r"depreciation and amortization|\bd&a\b", ("depreciation amortization",)),
    (r"net income", ("net income net earnings net loss",)),
    (r"total current assets", ("total current assets",)),
    (r"total current liabilities", ("total current liabilities",)),
    (r"total assets", ("total assets",)),
    (r"accounts payable", ("accounts payable trade payables",)),
    (r"inventor(?:y|ies)", ("inventory inventories",)),
    (r"operating income", ("operating income income from operations operating profit",)),
    (r"revenue", ("revenue net sales total revenues",)),
)


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def expand_query(query: str) -> str:
    normalized = " ".join(tokenize(query))
    additions: list[str] = []
    for phrase, aliases in QUERY_ALIASES.items():
        if phrase in normalized or any(alias in normalized for alias in aliases):
            additions.extend(aliases)
    return " ".join([query, *additions])


def extract_pdf_pages(path: Path, *, executable: str = "pdftotext") -> list[str]:
    result = subprocess.run(
        [executable, "-layout", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    )
    return split_pdf_pages(result.stdout)


def split_pdf_pages(text: str) -> list[str]:
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return [normalize_page(page) for page in pages]


def normalize_page(text: str) -> str:
    text = text.replace("\x00", "").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n", text)
    return text.strip()


def chunk_pages(
    pages: Iterable[str],
    *,
    max_chars: int = 1800,
    overlap_chars: int = 240,
) -> list[DocumentChunk]:
    chunks: list[DocumentChunk] = []
    for page_number, page in enumerate(pages, start=1):
        blocks = [block.strip() for block in re.split(r"\n\s*\n", page) if block.strip()]
        if not blocks:
            continue
        current = ""
        part = 0
        for block in blocks:
            pieces = hard_split(block, max_chars)
            for piece in pieces:
                candidate = f"{current}\n\n{piece}".strip() if current else piece
                if current and len(candidate) > max_chars:
                    chunks.append(
                        DocumentChunk(
                            chunk_id=f"p{page_number:04d}-c{part:03d}",
                            page=page_number,
                            text=current,
                            section_hint=infer_section_hint(current),
                        )
                    )
                    part += 1
                    current = f"{current[-overlap_chars:]}\n{piece}".strip()
                else:
                    current = candidate
        if current:
            chunks.append(
                DocumentChunk(
                    chunk_id=f"p{page_number:04d}-c{part:03d}",
                    page=page_number,
                    text=current,
                    section_hint=infer_section_hint(current),
                )
            )
    return chunks


def hard_split(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    lines = text.splitlines()
    pieces: list[str] = []
    current = ""
    for line in lines:
        if len(line) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(line[start : start + max_chars] for start in range(0, len(line), max_chars))
        elif current and len(current) + len(line) + 1 > max_chars:
            pieces.append(current)
            current = line
        else:
            current = f"{current}\n{line}".strip()
    if current:
        pieces.append(current)
    return pieces


def infer_section_hint(text: str) -> str:
    for line in text.splitlines()[:12]:
        clean = " ".join(line.split()).strip(" .:-")
        if 4 <= len(clean) <= 100 and (
            clean.isupper()
            or re.search(r"\b(statement|results|operations|risk factors|business|notes?)\b", clean, re.I)
        ):
            return clean
    return ""


class BM25Index:
    def __init__(self, chunks: list[DocumentChunk], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.tokens = [tokenize(chunk.text) for chunk in chunks]
        self.term_counts = [Counter(tokens) for tokens in self.tokens]
        self.avg_length = sum(map(len, self.tokens)) / max(1, len(self.tokens))
        document_frequency: Counter[str] = Counter()
        for tokens in self.tokens:
            document_frequency.update(set(tokens))
        count = max(1, len(chunks))
        self.idf = {
            term: math.log(1 + (count - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def search(self, query: str, *, limit: int = 10, expand: bool = True) -> list[DocumentChunk]:
        terms = tokenize(expand_query(query) if expand else query)
        scores: list[tuple[float, int]] = []
        for index, counts in enumerate(self.term_counts):
            length = len(self.tokens[index])
            score = 0.0
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * length / max(1.0, self.avg_length)
                )
                score += self.idf.get(term, 0.0) * frequency * (self.k1 + 1) / denominator
            if score > 0:
                scores.append((score, index))
        scores.sort(reverse=True)
        return [replace(self.chunks[index], score=round(score, 6)) for score, index in scores[:limit]]


def financial_query_facets(query: str) -> list[str]:
    years = " ".join(
        dict.fromkeys(re.findall(r"\b(?:FY\s*)?((?:19|20)\d{2})\b", query, flags=re.IGNORECASE))
    )
    facets: list[str] = []
    for pattern, phrases in FINANCIAL_QUERY_FACETS:
        if re.search(pattern, query, flags=re.IGNORECASE):
            facets.extend(f"{phrase} {years}".strip() for phrase in phrases)
    if not facets:
        facets.append(distill_financial_query(query))
    return list(dict.fromkeys(facets))


def distill_financial_query(query: str) -> str:
    stopwords = {
        "a", "an", "and", "answer", "as", "assuming", "based", "basing", "be", "best", "by",
        "calculate", "can", "clearly", "compute", "considering", "details", "did", "do", "end",
        "following", "from", "give", "have", "help", "how", "in", "information", "is", "it", "judgment",
        "much", "of", "only", "outlined", "perspective", "please", "primarily", "provided", "question",
        "reasonable", "referencing", "respond", "response", "shown", "state", "stated", "the", "this", "to",
        "using", "utilizing", "was", "we", "what", "when", "within", "year", "you",
    }
    return " ".join(token for token in tokenize(query) if token not in stopwords)


def focused_financial_search(
    index: BM25Index,
    query: str,
    *,
    limit: int = 80,
    per_query: int = 16,
) -> tuple[list[DocumentChunk], list[DocumentChunk]]:
    queries = [distill_financial_query(query), *financial_query_facets(query)]
    reciprocal_scores: dict[str, float] = Counter()
    chunks_by_id: dict[str, DocumentChunk] = {}
    anchors: list[DocumentChunk] = []
    for focused_query in dict.fromkeys(item for item in queries if item.strip()):
        matches = index.search(focused_query, limit=per_query, expand=True)
        if matches:
            anchors.append(matches[0])
        for rank, chunk in enumerate(matches, start=1):
            reciprocal_scores[chunk.chunk_id] += 1 / (30 + rank)
            chunks_by_id[chunk.chunk_id] = chunk
    ranked = sorted(
        (
            replace(chunks_by_id[chunk_id], score=round(score, 6))
            for chunk_id, score in reciprocal_scores.items()
        ),
        key=lambda chunk: chunk.score,
        reverse=True,
    )[:limit]
    return ranked, deduplicate_chunks(anchors)


def include_anchor_chunks(
    ranked: list[DocumentChunk],
    anchors: list[DocumentChunk],
    *,
    limit: int,
) -> list[DocumentChunk]:
    required = deduplicate_chunks(anchors)[:limit]
    required_ids = {chunk.chunk_id for chunk in required}
    selected = deduplicate_chunks(ranked)[:limit]
    selected_ids = {chunk.chunk_id for chunk in selected}
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


def deduplicate_chunks(chunks: Iterable[DocumentChunk]) -> list[DocumentChunk]:
    unique: dict[str, DocumentChunk] = {}
    for chunk in chunks:
        unique.setdefault(chunk.chunk_id, chunk)
    return list(unique.values())


def rerank_with_embeddings(
    query: str,
    candidates: list[DocumentChunk],
    embed: Callable[[list[str]], list[list[float]]],
    *,
    lexical_weight: float = 0.55,
    limit: int = 6,
) -> list[DocumentChunk]:
    if not candidates:
        return []
    vectors = embed([query, *[candidate.text for candidate in candidates]])
    if len(vectors) != len(candidates) + 1:
        raise ValueError("Embedding provider returned an unexpected vector count")
    lexical_max = max(candidate.score for candidate in candidates) or 1.0
    ranked: list[DocumentChunk] = []
    for candidate, vector in zip(candidates, vectors[1:], strict=True):
        lexical = candidate.score / lexical_max
        semantic = max(0.0, cosine_similarity(vectors[0], vector))
        score = lexical_weight * lexical + (1 - lexical_weight) * semantic
        ranked.append(replace(candidate, score=round(score, 6)))
    return sorted(ranked, key=lambda item: item.score, reverse=True)[:limit]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)
