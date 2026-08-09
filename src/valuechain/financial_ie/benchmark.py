from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from valuechain.embeddings import EmbeddingConfig, OpenAIEmbeddingClient
from valuechain.financial_ie.llm import AsyncLLMClient, AsyncLLMConfig
from valuechain.financial_ie.json_utils import parse_json_payload
from valuechain.financial_ie.models import BenchmarkCase, DocumentChunk
from valuechain.financial_ie.prompts import build_benchmark_prompt
from valuechain.financial_ie.retrieval import (
    BM25Index,
    chunk_pages,
    extract_pdf_pages,
    focused_financial_search,
    include_anchor_chunks,
    rerank_with_embeddings,
)
from valuechain.financial_ie.scoring import score_prediction


BENCHMARK_RUNNER_VERSION = "financial-ie-benchmark-v0.3"
BENCHMARK_SCORER_VERSION = "financial-ie-scorer-v0.2"


# Observed FIRE schema signatures. These are structural constraints, not text
# heuristics: a candidate relation with incompatible endpoint types cannot be a
# valid FIRE relation. Keeping this in code prevents prompt drift and makes the
# validator independently testable.
FIRE_RELATION_SIGNATURES: dict[str, set[tuple[str, str]]] = {
    "ActionBuy": {
        (actor, target)
        for actor in ("Company", "Person")
        for target in ("Company", "FinancialEntity", "BusinessUnit", "Product")
    },
    "ActionSell": {
        (actor, target)
        for actor in ("Company", "Person")
        for target in ("Company", "FinancialEntity", "BusinessUnit", "Product")
    },
    "ActionMerge": {("Company", "Company")},
    "Actionin": {("Action", "Date")},
    "Actionto": {
        ("Action", "Company"),
        ("Action", "FinancialEntity"),
        ("Action", "BusinessUnit"),
    },
    "Constituentof": {
        ("FinancialEntity", "FinancialEntity"),
        ("BusinessUnit", "Company"),
        ("BusinessUnit", "BusinessUnit"),
    },
    "Designation": {("Person", "Designation"), ("Company", "Designation")},
    "Employeeof": {("Person", "Company"), ("Person", "BusinessUnit")},
    "Locatedin": {
        (head, tail)
        for head in ("Company", "BusinessUnit", "Location", "GeopoliticalEntity", "Money", "Quantity")
        for tail in ("Location", "GeopoliticalEntity")
    },
    "Productof": {("Product", "Company")},
    "Propertyof": {
        ("Action", "Company"),
        ("Action", "Product"),
        ("Action", "Person"),
        ("Action", "BusinessUnit"),
        ("Action", "FinancialEntity"),
        ("FinancialEntity", "Product"),
        ("FinancialEntity", "FinancialEntity"),
        ("FinancialEntity", "Company"),
        ("FinancialEntity", "BusinessUnit"),
        ("FinancialEntity", "Person"),
        ("BusinessUnit", "Company"),
        ("BusinessUnit", "BusinessUnit"),
    },
    "Quantity": {
        ("FinancialEntity", "Quantity"),
        ("BusinessUnit", "Quantity"),
        ("Product", "Quantity"),
    },
    "Sector": {("Company", "Sector")},
    "Subsidiaryof": {("Company", "Company")},
    "Value": {
        ("FinancialEntity", "Money"),
        ("FinancialEntity", "Quantity"),
        ("BusinessUnit", "Money"),
        ("BusinessUnit", "Quantity"),
        ("Product", "Money"),
        ("Product", "Quantity"),
        ("Company", "Money"),
    },
    "ValueChangeDecreaseby": {
        ("FinancialEntity", "Money"),
        ("FinancialEntity", "Quantity"),
        ("BusinessUnit", "Quantity"),
    },
    "ValueChangeIncreaseby": {
        ("FinancialEntity", "Money"),
        ("FinancialEntity", "Quantity"),
        ("BusinessUnit", "Quantity"),
    },
    "Valuein": {("Money", "Date"), ("Quantity", "Date")},
}


@dataclass(frozen=True, slots=True)
class BenchmarkRunConfig:
    output_dir: Path
    model: str
    style: str
    base_url: str = "http://100.114.26.88:31969/v1"
    api_key: str = "1969"
    concurrency: int = 4
    use_embeddings: bool = True
    embedding_model: str = "qwen3-embed-0.6b"


class BenchmarkRunner:
    def __init__(self, config: BenchmarkRunConfig) -> None:
        self.config = config
        self._pdf_cache: dict[str, list[DocumentChunk]] = {}
        self._embedding_client = OpenAIEmbeddingClient(
            EmbeddingConfig(
                base_url=config.base_url,
                api_key=config.api_key,
                model=config.embedding_model,
                batch_size=32,
            )
        )

    async def run(self, cases: list[BenchmarkCase]) -> dict[str, Any]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        results_path = self.config.output_dir / "predictions.jsonl"
        completed = load_completed_case_ids(results_path)
        pending = [case for case in cases if case.case_id not in completed]
        lock = asyncio.Lock()
        llm_config = AsyncLLMConfig(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            model=self.config.model,
            concurrency=self.config.concurrency,
        )
        async with AsyncLLMClient(llm_config) as client:
            async def execute(case: BenchmarkCase) -> None:
                row = await self._run_case(case, client)
                async with lock:
                    append_jsonl(results_path, row)

            await asyncio.gather(*(execute(case) for case in pending))
        rows = rescore_rows(read_jsonl(results_path))
        write_jsonl(results_path, rows)
        summary = summarize_results(rows)
        summary.update(
            {
                "runner_version": BENCHMARK_RUNNER_VERSION,
                "scorer_version": BENCHMARK_SCORER_VERSION,
                "model": self.config.model,
                "style": self.config.style,
                "case_count": len(rows),
                "completed_at": datetime.now(UTC).isoformat(),
            }
        )
        (self.config.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return summary

    async def _run_case(self, case: BenchmarkCase, client: AsyncLLMClient) -> dict[str, Any]:
        retrieved: list[DocumentChunk] | None = None
        retrieval_metrics: dict[str, Any] = {}
        if self.config.style in {"retrieval", "workflow", "workflow_v2"} and case.task == "financebench":
            retrieved, retrieval_metrics = await self._retrieve_financebench(case)
        if self.config.style == "workflow" and case.task == "fire_joint_re":
            return await self._run_fire_workflow(case, client)
        if self.config.style == "workflow_v2" and case.task == "fire_joint_re":
            return await self._run_fire_workflow_v2(case, client)
        prompt_style = (
            "structured"
            if self.config.style in {"retrieval", "workflow", "workflow_v2"}
            else self.config.style
        )
        system, user, max_tokens = build_benchmark_prompt(
            case,
            style=prompt_style,
            retrieved_chunks=retrieved,
        )
        try:
            response = await client.complete(system, user, max_tokens=max_tokens)
            scores = score_prediction(case, response.content)
            if retrieved is not None and case.task == "financebench":
                scores.setdefault("citation_page_hit", 0)
            error = ""
            content = response.content
            latency_s = response.latency_s
            usage = response.usage
            attempts = response.attempts
        except Exception as exc:
            scores = {}
            error = f"{type(exc).__name__}: {exc}"
            content = ""
            latency_s = 0.0
            usage = {}
            attempts = 0
        return {
            "case_id": case.case_id,
            "task": case.task,
            "source": case.source,
            "question": case.question,
            "input_text": case.text,
            "model": self.config.model,
            "style": self.config.style,
            "gold": case.gold,
            "metadata": case.metadata,
            "prediction": content,
            "scores": scores,
            "retrieval": retrieval_metrics,
            "retrieved_chunks": [chunk.to_dict() for chunk in retrieved or []],
            "prompt_sha256": hashlib.sha256(f"{system}\n{user}".encode()).hexdigest(),
            "latency_s": latency_s,
            "usage": usage,
            "attempts": attempts,
            "error": error,
        }

    async def _run_fire_workflow(
        self,
        case: BenchmarkCase,
        client: AsyncLLMClient,
    ) -> dict[str, Any]:
        entity_types = ", ".join(case.metadata["entity_types"])
        entity_user = f"""Extract every benchmark entity mention as an exact source span.
Types: {entity_types}
Action includes explicit transaction verbs. Designation includes job and contract roles. FinancialEntity includes
financial measures and instruments. Money is a currency amount; Quantity is a percentage or count; Date includes
explicit periods; Sector is an industry phrase. Include repeated mentions.
Return {{"entities":[{{"text":"exact span","type":"type"}}]}}.

TEXT:
{case.text}"""
        first = await client.complete(
            "You are a strict financial joint-extraction engine. Return JSON only.",
            entity_user,
            max_tokens=1000,
        )
        try:
            entity_payload = parse_json_payload(first.content)
        except ValueError:
            entity_payload = {}
        entities = entity_payload.get("entities", []) if isinstance(entity_payload, dict) else []
        relation_types = ", ".join(case.metadata["relation_types"])
        relation_user = f"""Given source text and already extracted entities, extract all directed relations.
Allowed types: {relation_types}
Directions: component->aggregate Constituentof; product->company Productof; person->company Employeeof;
financial item->amount Value/Quantity; amount->date Valuein; company->sector Sector; company->designation
Designation; transaction verb->date Actionin; buyer->target ActionBuy; action verb->target Actionto.
Only use endpoint spans from the supplied entity list.
Return {{"relations":[{{"head":"entity span","tail":"entity span","type":"type"}}]}}.

ENTITIES:
{json.dumps(entities, ensure_ascii=False)}

TEXT:
{case.text}"""
        second = await client.complete(
            "You are a strict financial relation extraction engine. Return JSON only.",
            relation_user,
            max_tokens=1000,
        )
        try:
            relation_payload = parse_json_payload(second.content)
        except ValueError:
            relation_payload = {}
        relations = relation_payload.get("relations", []) if isinstance(relation_payload, dict) else []
        content = json.dumps({"entities": entities, "relations": relations}, ensure_ascii=False)
        scores = score_prediction(case, content)
        return {
            "case_id": case.case_id,
            "task": case.task,
            "source": case.source,
            "question": case.question,
            "input_text": case.text,
            "model": self.config.model,
            "style": self.config.style,
            "gold": case.gold,
            "metadata": case.metadata,
            "prediction": content,
            "intermediate_predictions": {
                "entities": first.content,
                "relations": second.content,
            },
            "scores": scores,
            "retrieval": {},
            "retrieved_chunks": [],
            "prompt_sha256": hashlib.sha256(f"{entity_user}\n{relation_user}".encode()).hexdigest(),
            "latency_s": round(first.latency_s + second.latency_s, 4),
            "usage": merge_usage(first.usage, second.usage),
            "attempts": first.attempts + second.attempts,
            "error": "",
        }

    async def _run_fire_workflow_v2(
        self,
        case: BenchmarkCase,
        client: AsyncLLMClient,
    ) -> dict[str, Any]:
        tokens = [str(token) for token in case.metadata.get("tokens", case.text.split())]
        entity_types = ", ".join(case.metadata["entity_types"])
        few_shot_examples = case.metadata.get("few_shot_examples", [])
        ner_examples = render_fire_ner_examples(few_shot_examples)
        entity_user = f"""Extract every FIRE benchmark entity mention as an exact source span.

Return JSON only:
{{"entities":[{{"text":"exact source span","type":"Company"}}]}}
Allowed entity types: {entity_types}

Rules:
- Copy each `text` exactly from the source. Use the complete noun phrase and do not paraphrase.
- Action is the explicit transaction/event word, not every ordinary verb.
- Product is a named or described good/service offered by a company; keep its full descriptive phrase.
- FinancialEntity is a financial measure, account, security, asset, liability, revenue/cost item, or investment.
- Designation is a person's job role or a company's contractual/competitive role.
- Money is a currency amount; Quantity is a percentage, count, multiple, or non-currency measure.
- Sector is an industry phrase; BusinessUnit is an organizational division.
- Include repeated mentions separately. Do not invent implicit entities.

TEXT:
{case.text}

TRAINING EXAMPLES:
{ner_examples}"""
        first = await client.complete(
            "You are a strict financial NER engine. Return valid JSON only.",
            entity_user,
            max_tokens=1200,
        )
        try:
            entity_payload = parse_json_payload(first.content)
        except ValueError:
            entity_payload = {}
        raw_entities = entity_payload.get("entities", []) if isinstance(entity_payload, dict) else []
        internal_entities = align_fire_text_entities(
            raw_entities,
            tokens,
            set(case.metadata["entity_types"]),
        )

        relation_types = ", ".join(case.metadata["relation_types"])
        relation_examples = render_fire_relation_examples(few_shot_examples)
        relation_user = f"""Extract every explicitly supported directed FIRE relation from the text.
Use only entity IDs from ENTITY CATALOG; never output text spans as endpoints.
Return JSON only:
{{"relations":[{{"head_id":"e0","type":"Employeeof","tail_id":"e1"}}]}}
Allowed relation types: {relation_types}

Direction and meaning:
- Productof: Product -> Company that offers/makes it.
- Employeeof: Person -> employing Company/BusinessUnit.
- Subsidiaryof: subsidiary Company -> parent Company.
- Sector: Company -> industry Sector. Designation: Person/Company -> role.
- Propertyof: financial item/action/business unit -> its owner or subject.
- Constituentof: component financial item/business unit -> larger aggregate/company.
- Value or Quantity: measured item -> amount. Valuein: amount -> Date.
- ValueChangeIncreaseby / ValueChangeDecreaseby: changing financial item -> delta amount.
- Actionin: Action -> Date. Actionto: Action verb -> its target.
- ActionBuy / ActionSell: buyer or seller -> acquired/sold target. ActionMerge joins two companies.
- Locatedin: entity/location/amount -> stated geographic location.

Precision rules:
- A valid type signature is mandatory; do not reverse an edge.
- Extract only relations directly stated by this sentence, not plausible business knowledge.
- Do not turn ordinary descriptive verbs into Action unless the sentence presents a financial/corporate event.
- It is valid to return an empty list.

ENTITY CATALOG:
{json.dumps(internal_entities, ensure_ascii=False)}

TEXT:
{case.text}

TRAINING EXAMPLES:
{relation_examples}"""
        second = await client.complete(
            "You are a strict financial relation extraction engine. Return valid JSON only.",
            relation_user,
            max_tokens=1200,
        )
        try:
            relation_payload = parse_json_payload(second.content)
        except ValueError:
            relation_payload = {}
        raw_relations = relation_payload.get("relations", []) if isinstance(relation_payload, dict) else []
        relations = normalize_fire_id_relations(
            raw_relations,
            internal_entities,
            set(case.metadata["relation_types"]),
        )
        entities = [
            {"text": entity["text"], "type": entity["type"]}
            for entity in internal_entities
        ]
        content = json.dumps({"entities": entities, "relations": relations}, ensure_ascii=False)
        scores = score_prediction(case, content)
        return {
            "case_id": case.case_id,
            "task": case.task,
            "source": case.source,
            "question": case.question,
            "input_text": case.text,
            "model": self.config.model,
            "style": self.config.style,
            "gold": case.gold,
            "metadata": case.metadata,
            "prediction": content,
            "intermediate_predictions": {
                "entities": first.content,
                "normalized_entities": internal_entities,
                "relations": second.content,
            },
            "scores": scores,
            "retrieval": {},
            "retrieved_chunks": [],
            "prompt_sha256": hashlib.sha256(f"{entity_user}\n{relation_user}".encode()).hexdigest(),
            "latency_s": round(first.latency_s + second.latency_s, 4),
            "usage": merge_usage(first.usage, second.usage),
            "attempts": first.attempts + second.attempts,
            "error": "",
        }

    async def _retrieve_financebench(
        self, case: BenchmarkCase
    ) -> tuple[list[DocumentChunk], dict[str, Any]]:
        pdf_path = Path(str(case.metadata["pdf_path"]))
        cache_key = str(pdf_path.resolve())
        if cache_key not in self._pdf_cache:
            pages = await asyncio.to_thread(extract_pdf_pages, pdf_path)
            self._pdf_cache[cache_key] = chunk_pages(pages)
        chunks = self._pdf_cache[cache_key]
        lexical, anchors = focused_financial_search(BM25Index(chunks), case.question)
        ranked = lexical
        if self.config.use_embeddings and lexical:
            ranked = await asyncio.to_thread(
                rerank_with_embeddings,
                case.question,
                lexical,
                self._embedding_client.embed_texts,
                lexical_weight=0.35,
                limit=16,
            )
        ranked = include_anchor_chunks(ranked, anchors, limit=12)
        gold_pages = {int(page) for page in case.metadata.get("evidence_pages", [])}
        return ranked, {
            "chunk_count": len(chunks),
            "gold_pages": sorted(gold_pages),
            "lexical_page_hit_at_3": page_hit(lexical[:3], gold_pages),
            "lexical_page_hit_at_8": page_hit(lexical[:8], gold_pages),
            "final_page_hit_at_3": page_hit(ranked[:3], gold_pages),
            "final_page_hit_at_8": page_hit(ranked[:8], gold_pages),
            "final_page_hit_at_12": page_hit(ranked[:12], gold_pages),
            "final_near_page_hit_at_8": page_hit(ranked[:8], gold_pages, tolerance=1),
        }


def summarize_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("task"))].append(row)
    task_summaries: dict[str, Any] = {}
    for task, task_rows in sorted(groups.items()):
        numeric: dict[str, list[float]] = defaultdict(list)
        counts: dict[str, float] = defaultdict(float)
        total_latency = 0.0
        errors = 0
        for row in task_rows:
            if row.get("error"):
                errors += 1
            total_latency += float(row.get("latency_s") or 0)
            for namespace in (row.get("scores") or {}, row.get("retrieval") or {}):
                for key, value in namespace.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        if key in {
                            "tp", "fp", "fn", "entity_tp", "entity_fp", "entity_fn",
                            "relation_tp", "relation_fp", "relation_fn",
                        }:
                            counts[key] += float(value)
                        elif key not in {"predicted_answer", "tool_answer"}:
                            numeric[key].append(float(value))
        micro = {}
        for prefix in ("", "entity_", "relation_"):
            tp = counts.get(f"{prefix}tp", 0.0)
            fp = counts.get(f"{prefix}fp", 0.0)
            fn = counts.get(f"{prefix}fn", 0.0)
            if tp + fp + fn:
                precision = tp / (tp + fp) if tp + fp else 0.0
                recall = tp / (tp + fn) if tp + fn else 0.0
                f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
                micro[f"{prefix}precision"] = round(precision, 6)
                micro[f"{prefix}recall"] = round(recall, 6)
                micro[f"{prefix}f1"] = round(f1, 6)
        task_summaries[task] = {
            "count": len(task_rows),
            "errors": errors,
            "avg_latency_s": round(total_latency / max(1, len(task_rows)), 4),
            "metrics": {
                key: round(sum(values) / len(values), 6)
                for key, values in sorted(numeric.items())
                if values
            },
            "metric_counts": {
                key: len(values)
                for key, values in sorted(numeric.items())
                if values
            },
            "micro_metrics": micro,
        }
    return {"tasks": task_summaries}


def page_hit(chunks: list[DocumentChunk], gold_pages: set[int], *, tolerance: int = 0) -> int:
    return int(
        any(
            chunk.page is not None and any(abs(chunk.page - page) <= tolerance for page in gold_pages)
            for chunk in chunks
        )
    )


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_completed_case_ids(path: Path) -> set[str]:
    return {str(row.get("case_id")) for row in read_jsonl(path) if not row.get("error")}


def rescore_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        if row.get("error"):
            continue
        case = BenchmarkCase(
            case_id=str(row.get("case_id") or ""),
            task=str(row.get("task") or ""),
            source=str(row.get("source") or ""),
            text=str(row.get("input_text") or ""),
            question=str(row.get("question") or ""),
            gold=row.get("gold"),
            metadata=row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
        )
        row["scores"] = score_prediction(case, str(row.get("prediction") or ""))
        if row.get("retrieved_chunks") and case.task == "financebench":
            row["scores"].setdefault("citation_page_hit", 0)
        row["scorer_version"] = BENCHMARK_SCORER_VERSION
    return rows


def merge_usage(*rows: dict[str, Any]) -> dict[str, int]:
    keys = {key for row in rows for key, value in row.items() if isinstance(value, (int, float))}
    return {key: int(sum(float(row.get(key) or 0) for row in rows)) for key in keys}


def normalize_fire_id_relations(
    rows: Any,
    entities: list[dict[str, Any]],
    allowed_types: set[str],
) -> list[dict[str, str]]:
    entity_by_id = {str(entity["id"]): entity for entity in entities}
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    if not isinstance(rows, list):
        return normalized
    for row in rows:
        if not isinstance(row, dict):
            continue
        head = entity_by_id.get(str(row.get("head_id") or ""))
        tail = entity_by_id.get(str(row.get("tail_id") or ""))
        relation_type = str(row.get("type") or "").strip()
        if head is None or tail is None or relation_type not in allowed_types or head["id"] == tail["id"]:
            continue
        valid_signatures = FIRE_RELATION_SIGNATURES.get(relation_type, set())
        if (str(head["type"]), str(tail["type"])) not in valid_signatures:
            continue
        key = (str(head["id"]), str(tail["id"]), relation_type)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "head": str(head["text"]),
                "tail": str(tail["text"]),
                "type": relation_type,
            }
        )
    return normalized


def align_fire_text_entities(
    rows: Any,
    tokens: list[str],
    allowed_types: set[str],
) -> list[dict[str, Any]]:
    """Align exact-text LLM mentions to FIRE tokens and assign stable IDs.

    Repeated identical mentions are assigned to successive unused occurrences.
    Unalignable or paraphrased mentions are rejected instead of contaminating
    relation endpoints with model-generated strings.
    """
    normalized_tokens = [normalize_fire_token(token) for token in tokens]
    candidates: list[dict[str, Any]] = []
    occupied: set[tuple[int, int, str]] = set()
    if not isinstance(rows, list):
        return candidates
    for row in rows:
        if not isinstance(row, dict):
            continue
        entity_type = str(row.get("type") or "").strip()
        mention_tokens = [
            normalize_fire_token(token)
            for token in str(row.get("text") or "").strip().split()
            if normalize_fire_token(token)
        ]
        if entity_type not in allowed_types or not mention_tokens:
            continue
        matches = [
            (start, start + len(mention_tokens))
            for start in range(len(tokens) - len(mention_tokens) + 1)
            if normalized_tokens[start : start + len(mention_tokens)] == mention_tokens
        ]
        match = next(
            (
                (start, end)
                for start, end in matches
                if (start, end, entity_type) not in occupied
            ),
            None,
        )
        if match is None:
            continue
        start, end = match
        occupied.add((start, end, entity_type))
        candidates.append(
            {
                "id": "",
                "start": start,
                "end": end,
                "text": " ".join(tokens[start:end]),
                "type": entity_type,
            }
        )
    candidates.sort(key=lambda item: (item["start"], item["end"], item["type"]))
    for index, entity in enumerate(candidates):
        entity["id"] = f"e{index}"
    return candidates


def normalize_fire_token(token: str) -> str:
    return token.strip().casefold()


def render_fire_ner_examples(examples: Any) -> str:
    if not isinstance(examples, list) or not examples:
        return "(none)"
    blocks = []
    for example in examples:
        if not isinstance(example, dict):
            continue
        entities = [
            {"text": entity.get("text"), "type": entity.get("type")}
            for entity in example.get("entities", [])
            if isinstance(entity, dict)
        ]
        blocks.append(
            f"INPUT: {example.get('text', '')}\nOUTPUT: "
            + json.dumps({"entities": entities}, ensure_ascii=False)
        )
    return "\n\n".join(blocks) or "(none)"


def render_fire_relation_examples(examples: Any) -> str:
    if not isinstance(examples, list) or not examples:
        return "(none)"
    blocks = []
    for example in examples:
        if not isinstance(example, dict):
            continue
        blocks.append(
            f"TEXT: {example.get('text', '')}\nENTITY CATALOG: "
            + json.dumps(example.get("entities", []), ensure_ascii=False)
            + "\nOUTPUT: "
            + json.dumps({"relations": example.get("relations", [])}, ensure_ascii=False)
        )
    return "\n\n".join(blocks) or "(none)"
