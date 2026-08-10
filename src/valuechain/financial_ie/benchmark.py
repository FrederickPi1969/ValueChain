from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from valuechain.embeddings import EmbeddingConfig, OpenAIEmbeddingClient
from valuechain.financial_ie.llm import AsyncLLMClient, AsyncLLMConfig, LLMResponse
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


BENCHMARK_RUNNER_VERSION = "financial-ie-benchmark-v0.5"
BENCHMARK_SCORER_VERSION = "financial-ie-scorer-v0.3"


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

# The audit pass helps boundary-heavy semantic categories, but on the pilot it
# over-edits already reliable literal categories such as Money, Date, and
# Person. Replace only the categories for which review adds information.
FIRE_NER_REVIEW_REPLACE_TYPES = {"Company", "FinancialEntity", "Product", "Sector"}

FIRE_NER_SPLIT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "literal named entities and values",
        (
            "Company",
            "Person",
            "Location",
            "GeopoliticalEntity",
            "Money",
            "Quantity",
            "Date",
        ),
    ),
    (
        "financial concepts, products, organization roles, and events",
        (
            "FinancialEntity",
            "Product",
            "BusinessUnit",
            "Sector",
            "Action",
            "Designation",
        ),
    ),
)

FIRE_RELATION_VERIFY_TYPES = {
    "Productof",
    "Propertyof",
    "Constituentof",
    "ActionSell",
    "Sector",
    "Quantity",
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
    fire_use_gold_entities: bool = False
    fire_mark_entities: bool = True
    fire_candidate_pairs: bool = False
    fire_entity_predictions_path: Path | None = None
    fire_ner_review: bool = False
    fire_ner_strategy: str = "single"
    fire_ner_completion: bool = False
    fire_relation_entity_recovery: bool = False
    fire_alias_rescan_path: Path | None = None
    fire_relation_verifier: bool = False


class BenchmarkRunner:
    def __init__(self, config: BenchmarkRunConfig) -> None:
        self.config = config
        self._pdf_cache: dict[str, list[DocumentChunk]] = {}
        self._fire_entity_predictions = load_fire_entity_predictions(
            config.fire_entity_predictions_path
        )
        self._fire_alias_lexicon = load_fire_training_alias_lexicon(
            config.fire_alias_rescan_path
        )
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
                "fire_use_gold_entities": self.config.fire_use_gold_entities,
                "fire_mark_entities": self.config.fire_mark_entities,
                "fire_candidate_pairs": self.config.fire_candidate_pairs,
                "fire_entity_predictions_path": str(
                    self.config.fire_entity_predictions_path or ""
                ),
                "fire_ner_review": self.config.fire_ner_review,
                "fire_ner_strategy": self.config.fire_ner_strategy,
                "fire_ner_completion": self.config.fire_ner_completion,
                "fire_relation_entity_recovery": self.config.fire_relation_entity_recovery,
                "fire_alias_rescan_path": str(self.config.fire_alias_rescan_path or ""),
                "fire_relation_verifier": self.config.fire_relation_verifier,
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
        entity_user = build_fire_ner_prompt(
            case.text,
            tuple(case.metadata["entity_types"]),
            ner_examples,
        )
        cached_entities = self._fire_entity_predictions.get(case.case_id)
        entity_responses: list[LLMResponse] = []
        review_content = ""
        if cached_entities is not None:
            internal_entities = cached_entities
            first = LLMResponse(
                content=json.dumps({"entities": internal_entities}, ensure_ascii=False),
                latency_s=0.0,
                usage={},
                attempts=0,
            )
            entity_responses.append(first)
        elif self.config.fire_use_gold_entities:
            internal_entities = fire_entities_from_spans(
                case.metadata.get("gold_entity_spans", []),
                tokens,
                set(case.metadata["entity_types"]),
            )
            first = LLMResponse(
                content=json.dumps({"entities": internal_entities}, ensure_ascii=False),
                latency_s=0.0,
                usage={},
                attempts=0,
            )
            entity_responses.append(first)
        elif self.config.fire_ner_strategy == "indexed":
            indexed_examples = render_fire_ner_examples(
                few_shot_examples,
                include_offsets=True,
            )
            indexed_user = build_fire_indexed_ner_prompt(
                tokens,
                tuple(case.metadata["entity_types"]),
                indexed_examples,
            )
            first = await client.complete(
                "You are a strict token-indexed financial NER engine. Return valid JSON only.",
                indexed_user,
                max_tokens=1200,
            )
            entity_responses.append(first)
            try:
                indexed_payload = parse_json_payload(first.content)
            except ValueError:
                indexed_payload = {}
            raw_indexed_entities = (
                indexed_payload.get("entities", [])
                if isinstance(indexed_payload, dict)
                else []
            )
            internal_entities = fire_entities_from_spans(
                raw_indexed_entities,
                tokens,
                set(case.metadata["entity_types"]),
            )
        elif self.config.fire_ner_strategy in {"split", "ensemble", "consensus"}:
            split_entities: list[dict[str, Any]] = []
            single_entities: list[dict[str, Any]] = []
            if self.config.fire_ner_strategy in {"ensemble", "consensus"}:
                single_response = await client.complete(
                    "You are a strict financial NER engine. Return valid JSON only.",
                    entity_user,
                    max_tokens=1200,
                )
                entity_responses.append(single_response)
                try:
                    single_payload = parse_json_payload(single_response.content)
                except ValueError:
                    single_payload = {}
                raw_single_entities = (
                    single_payload.get("entities", [])
                    if isinstance(single_payload, dict)
                    else []
                )
                single_entities = align_fire_text_entities(
                    raw_single_entities,
                    tokens,
                    set(case.metadata["entity_types"]),
                )
            for group_name, group_types in FIRE_NER_SPLIT_GROUPS:
                allowed_group_types = tuple(
                    entity_type
                    for entity_type in group_types
                    if entity_type in set(case.metadata["entity_types"])
                )
                group_examples = render_fire_ner_examples(
                    few_shot_examples,
                    allowed_types=set(allowed_group_types),
                )
                group_prompt = build_fire_ner_prompt(
                    case.text,
                    allowed_group_types,
                    group_examples,
                    focus=group_name,
                )
                response = await client.complete(
                    "You are a strict financial NER engine. Return valid JSON only.",
                    group_prompt,
                    max_tokens=900,
                )
                entity_responses.append(response)
                try:
                    payload = parse_json_payload(response.content)
                except ValueError:
                    payload = {}
                raw_group_entities = payload.get("entities", []) if isinstance(payload, dict) else []
                split_entities.extend(
                    align_fire_text_entities(
                        raw_group_entities,
                        tokens,
                        set(allowed_group_types),
                    )
                )
            merged_split_entities = merge_fire_entities(split_entities)
            if self.config.fire_ner_strategy == "ensemble":
                internal_entities = intersect_fire_entities(
                    single_entities,
                    merged_split_entities,
                )
            elif self.config.fire_ner_strategy == "consensus":
                union_entities = merge_fire_entities(single_entities + merged_split_entities)
                single_keys = {fire_entity_key(entity) for entity in single_entities}
                split_keys = {fire_entity_key(entity) for entity in merged_split_entities}
                verifier_catalog = [
                    {
                        **entity,
                        "support": (
                            "both"
                            if fire_entity_key(entity) in single_keys & split_keys
                            else "single"
                            if fire_entity_key(entity) in single_keys
                            else "split"
                        ),
                    }
                    for entity in union_entities
                ]
                disputed_ids = [
                    str(entity["id"])
                    for entity in verifier_catalog
                    if entity["support"] != "both"
                ]
                verifier_user = f"""Verify disputed FIRE entity candidates without changing their spans or types.

Return JSON only:
{{"accepted_ids":["e2","e5"]}}

Candidates with support=`both` are already accepted. Judge only IDs with support=`single` or support=`split`.
Accept a disputed ID only when its exact text span and assigned type satisfy the FIRE ontology in this sentence.
Reject partial noun phrases, surrounding context, ordinary verbs, generic unlabeled nouns, inferred mentions,
pronouns, and wrong types. Do not add entities or return IDs outside DISPUTED IDS.

Type reminders:
- Product is a complete offered good/service; Company is a named legal/commercial organization.
- FinancialEntity is a financial measure/account/security/asset/liability/revenue/cost item/investment.
- Action is an explicit financial/corporate transaction event word.
- Designation is a job, contractual, ownership, or competitive role.
- Sector is the industry phrase itself; BusinessUnit is an internal organizational division.

DISPUTED IDS:
{json.dumps(disputed_ids, ensure_ascii=False)}

CANDIDATE CATALOG:
{json.dumps(verifier_catalog, ensure_ascii=False)}

SOURCE TEXT:
{case.text}"""
                verifier = await client.complete(
                    "You are a conservative entity-candidate verifier. Return valid JSON only.",
                    verifier_user,
                    max_tokens=500,
                )
                entity_responses.append(verifier)
                try:
                    verifier_payload = parse_json_payload(verifier.content)
                except ValueError:
                    verifier_payload = {}
                accepted_ids = {
                    str(entity_id)
                    for entity_id in (
                        verifier_payload.get("accepted_ids", [])
                        if isinstance(verifier_payload, dict)
                        else []
                    )
                    if str(entity_id) in disputed_ids
                }
                internal_entities = merge_fire_entities(
                    [
                        entity
                        for entity in verifier_catalog
                        if entity["support"] == "both" or str(entity["id"]) in accepted_ids
                    ]
                )
            else:
                internal_entities = merged_split_entities
            first = entity_responses[0]
        else:
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
            entity_responses.append(first)
            if self.config.fire_ner_review:
                draft_entities = internal_entities
                review_user = f"""Audit and replace a draft FIRE entity extraction.

Return the complete corrected entity list as JSON only:
{{"entities":[{{"text":"exact source span","type":"Company"}}]}}
Allowed entity types: {entity_types}

Check every draft item, then inspect the source again for omissions. Correct boundaries and types; add missing
entities; remove invented entities. Copy every span verbatim. Pay special attention to:
- transaction/event words such as acquisition, acquire, sold, merger, transfer, and increase as Action;
- complete offered goods or services as Product, including descriptive service phrases;
- financial measures, accounts, securities, assets, liabilities, revenue/cost items, and investments as FinancialEntity;
- job, contractual, ownership, and competitive roles as Designation;
- the industry phrase itself as Sector, excluding trailing words like market or industry when they are not part of the label;
- organizational divisions as BusinessUnit. Do not treat an external company as a BusinessUnit.
Repeated source mentions remain separate. Do not create implicit entities or resolve pronouns.

DRAFT ENTITY CATALOG:
{json.dumps(internal_entities, ensure_ascii=False)}

DRAFT-MARKED TEXT:
{mark_fire_entities(tokens, internal_entities)}

SOURCE TEXT:
{case.text}

TRAINING EXAMPLES:
{ner_examples}"""
                review = await client.complete(
                    "You are a strict financial NER auditor. Return valid JSON only.",
                    review_user,
                    max_tokens=1400,
                )
                entity_responses.append(review)
                review_content = review.content
                try:
                    review_payload = parse_json_payload(review.content)
                except ValueError:
                    review_payload = {}
                reviewed_entities = (
                    review_payload.get("entities", [])
                    if isinstance(review_payload, dict)
                    else []
                )
                reviewed_entities = align_fire_text_entities(
                    reviewed_entities,
                    tokens,
                    set(case.metadata["entity_types"]),
                )
                internal_entities = merge_fire_entity_review(
                    draft_entities,
                    reviewed_entities,
                    replace_types=FIRE_NER_REVIEW_REPLACE_TYPES,
                )

        if self._fire_alias_lexicon:
            internal_entities = rescan_fire_aliases(
                tokens,
                internal_entities,
                self._fire_alias_lexicon,
            )

        completion_content = ""
        if self.config.fire_ner_completion:
            completion_user = f"""Find only entity mentions missing from a draft FIRE entity catalog.

Return JSON only:
{{"entities":[{{"text":"exact source span","type":"Sector"}}]}}
Allowed entity types: {entity_types}

Do not repeat or rewrite a draft entity. Add a mention only when it is an explicit endpoint needed for a relation
stated in the sentence. Check especially these patterns:
- Company -> industry phrase (Sector), and Product -> offering Company;
- Person -> job/contract role (Designation) and employing Company/BusinessUnit;
- financial measure/account/security/asset/revenue/cost item (FinancialEntity) -> Money or Quantity;
- BusinessUnit/Product -> Money or Quantity;
- transaction/event word (Action) -> Date or target entity;
- subsidiary Company -> parent Company, and entity -> geographic Location/GeopoliticalEntity.
Copy the complete span verbatim. Do not add pronouns, inferred entities, ordinary verbs, or generic nouns without
an explicit relation. If nothing is missing, return an empty list.

DRAFT ENTITY CATALOG:
{json.dumps(internal_entities, ensure_ascii=False)}

DRAFT-MARKED TEXT:
{mark_fire_entities(tokens, internal_entities)}

SOURCE TEXT:
{case.text}"""
            completion = await client.complete(
                "You are a conservative financial NER completion engine. Return valid JSON only.",
                completion_user,
                max_tokens=800,
            )
            entity_responses.append(completion)
            completion_content = completion.content
            try:
                completion_payload = parse_json_payload(completion.content)
            except ValueError:
                completion_payload = {}
            raw_completion_entities = (
                completion_payload.get("entities", [])
                if isinstance(completion_payload, dict)
                else []
            )
            completion_entities = align_fire_text_entities(
                raw_completion_entities,
                tokens,
                set(case.metadata["entity_types"]),
            )
            internal_entities = merge_fire_entities(internal_entities + completion_entities)

        recovery_content = ""
        if self.config.fire_relation_entity_recovery:
            relation_types = ", ".join(case.metadata["relation_types"])
            recovery_examples = render_fire_text_relation_examples(few_shot_examples)
            recovery_user = f"""Propose explicit FIRE relations to recover entity endpoints missing from a draft catalog.

Return JSON only:
{{"relations":[{{"head_text":"exact span","head_type":"Company","type":"Sector",\
"tail_text":"exact span","tail_type":"Sector"}}]}}
Allowed entity types: {entity_types}
Allowed relation types: {relation_types}

Copy complete endpoint spans verbatim. Output only relations directly stated in the sentence. Do not use pronouns,
implicit entities, or world knowledge. The relation and endpoint types must obey these directions:
Product->Company Productof; Person->Company/BusinessUnit Employeeof; Company->Sector Sector;
Person/Company->role Designation; subsidiary Company->parent Company Subsidiaryof;
financial item/business unit/product->Money/Quantity Value or Quantity;
Money/Quantity->Date Valuein; Action->Date Actionin; Action->target Actionto;
buyer/seller Company/Person->target ActionBuy/ActionSell; entity->place Locatedin.
It is valid to return an empty list.

DRAFT ENTITY CATALOG:
{json.dumps(internal_entities, ensure_ascii=False)}

SOURCE TEXT:
{case.text}

TRAINING EXAMPLES:
{recovery_examples}"""
            recovery = await client.complete(
                "You are a strict relation-conditioned entity recovery engine. Return valid JSON only.",
                recovery_user,
                max_tokens=1000,
            )
            entity_responses.append(recovery)
            recovery_content = recovery.content
            try:
                recovery_payload = parse_json_payload(recovery.content)
            except ValueError:
                recovery_payload = {}
            raw_recovery_relations = (
                recovery_payload.get("relations", [])
                if isinstance(recovery_payload, dict)
                else []
            )
            recovered_entities = fire_entities_from_relation_candidates(
                raw_recovery_relations,
                tokens,
                set(case.metadata["entity_types"]),
                set(case.metadata["relation_types"]),
            )
            internal_entities = merge_fire_entities(internal_entities + recovered_entities)

        relation_types = ", ".join(case.metadata["relation_types"])
        relation_examples = render_fire_relation_examples(few_shot_examples)
        marked_text = (
            mark_fire_entities(tokens, internal_entities)
            if self.config.fire_mark_entities
            else case.text
        )
        candidate_pairs = (
            build_fire_candidate_pairs(internal_entities)
            if self.config.fire_candidate_pairs
            else []
        )
        candidate_instruction = (
            "\nCandidate endpoint pairs and their legal labels are listed below. "
            "Evaluate every candidate; output only relations explicitly expressed by the sentence.\n"
            f"CANDIDATE PAIRS:\n{json.dumps(candidate_pairs, ensure_ascii=False)}\n"
            if candidate_pairs
            else ""
        )
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
{candidate_instruction}

ENTITY CATALOG:
{json.dumps(internal_entities, ensure_ascii=False)}

TEXT:
{marked_text}

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
        verifier_response: LLMResponse | None = None
        relation_verifier_content = ""
        if self.config.fire_relation_verifier:
            verify_candidates = [
                {"candidate_id": f"r{index}", **relation}
                for index, relation in enumerate(relations)
                if relation["type"] in FIRE_RELATION_VERIFY_TYPES
            ]
            if verify_candidates:
                verifier_user = f"""Verify ambiguous FIRE relation candidates against the source sentence.

Return JSON only:
{{"accepted_ids":["r0"]}}
Accept a candidate only when its exact directed relation is explicitly stated. Do not repair, reverse, or add a
relation. Productof means a Product -> the Company that offers/makes that exact product. Propertyof means the
financial item/action/business unit -> its explicit owner or subject, not mere co-occurrence. Constituentof means
a component -> the named aggregate. Sector means Company -> explicit industry phrase. Quantity means a measured
item -> its non-currency amount. ActionSell requires an explicit seller -> sold target.

ENTITY CATALOG:
{json.dumps(internal_entities, ensure_ascii=False)}

CANDIDATES:
{json.dumps(verify_candidates, ensure_ascii=False)}

TEXT:
{marked_text}"""
                verifier_response = await client.complete(
                    "You are a conservative relation verifier. Return valid JSON only.",
                    verifier_user,
                    max_tokens=500,
                )
                relation_verifier_content = verifier_response.content
                try:
                    verifier_payload = parse_json_payload(verifier_response.content)
                except ValueError:
                    verifier_payload = {}
                candidate_ids = {
                    str(candidate["candidate_id"]) for candidate in verify_candidates
                }
                accepted_ids = {
                    str(candidate_id)
                    for candidate_id in (
                        verifier_payload.get("accepted_ids", [])
                        if isinstance(verifier_payload, dict)
                        else []
                    )
                    if str(candidate_id) in candidate_ids
                }
                verified_keys = {
                    (
                        candidate["head_id"],
                        candidate["type"],
                        candidate["tail_id"],
                    )
                    for candidate in verify_candidates
                    if candidate["candidate_id"] in accepted_ids
                }
                relations = [
                    relation
                    for relation in relations
                    if relation["type"] not in FIRE_RELATION_VERIFY_TYPES
                    or (relation["head_id"], relation["type"], relation["tail_id"])
                    in verified_keys
                ]
        entities = internal_entities
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
                "entity_passes": [response.content for response in entity_responses],
                "entity_review": review_content,
                "entity_completion": completion_content,
                "relation_entity_recovery": recovery_content,
                "normalized_entities": internal_entities,
                "relations": second.content,
                "relation_verifier": relation_verifier_content,
            },
            "scores": scores,
            "retrieval": {},
            "retrieved_chunks": [],
            "prompt_sha256": hashlib.sha256(f"{entity_user}\n{relation_user}".encode()).hexdigest(),
            "latency_s": round(
                sum(response.latency_s for response in entity_responses)
                + second.latency_s
                + (verifier_response.latency_s if verifier_response else 0.0),
                4,
            ),
            "usage": merge_usage(
                *(response.usage for response in entity_responses),
                second.usage,
                *((verifier_response.usage,) if verifier_response else ()),
            ),
            "attempts": (
                sum(response.attempts for response in entity_responses)
                + second.attempts
                + (verifier_response.attempts if verifier_response else 0)
            ),
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


def load_fire_entity_predictions(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None or not path.exists():
        return {}
    predictions: dict[str, list[dict[str, Any]]] = {}
    for row in read_jsonl(path):
        intermediate = row.get("intermediate_predictions")
        entities = intermediate.get("normalized_entities") if isinstance(intermediate, dict) else None
        if not isinstance(entities, list):
            continue
        predictions[str(row.get("case_id") or "")] = [
            dict(entity) for entity in entities if isinstance(entity, dict)
        ]
    return predictions


def load_fire_training_alias_lexicon(
    path: Path | None,
    *,
    min_count: int = 2,
) -> dict[str, list[tuple[tuple[str, ...], str]]]:
    """Build a pure, repeated alias/type lexicon from FIRE's training split."""
    if path is None or not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    counts: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
    for row in rows if isinstance(rows, list) else []:
        tokens = [normalize_fire_token(str(token)) for token in row.get("tokens", [])]
        for entity in row.get("entities", []):
            if not isinstance(entity, dict):
                continue
            try:
                start = int(entity["start"])
                end = int(entity["end"])
            except (KeyError, TypeError, ValueError):
                continue
            alias = tuple(tokens[start:end])
            entity_type = str(entity.get("type") or "").strip()
            if alias and entity_type:
                counts[alias][entity_type] += 1
    by_first_token: dict[str, list[tuple[tuple[str, ...], str]]] = defaultdict(list)
    for alias, type_counts in counts.items():
        total = sum(type_counts.values())
        entity_type, top_count = type_counts.most_common(1)[0]
        if total < min_count or top_count != total:
            continue
        if len(alias) == 1 and len(alias[0]) < 4:
            continue
        by_first_token[alias[0]].append((alias, entity_type))
    for candidates in by_first_token.values():
        candidates.sort(key=lambda item: (-len(item[0]), item[0], item[1]))
    return dict(by_first_token)


def rescan_fire_aliases(
    tokens: list[str],
    entities: list[dict[str, Any]],
    lexicon: dict[str, list[tuple[tuple[str, ...], str]]],
) -> list[dict[str, Any]]:
    """Longest-match alias completion that never overwrites a model span."""
    normalized_tokens = [normalize_fire_token(token) for token in tokens]
    completed = [dict(entity) for entity in entities]
    occupied = {(int(entity["start"]), int(entity["end"])) for entity in completed}
    index = 0
    while index < len(tokens):
        match: tuple[int, int, str] | None = None
        for alias, entity_type in lexicon.get(normalized_tokens[index], []):
            end = index + len(alias)
            if tuple(normalized_tokens[index:end]) != alias:
                continue
            if any(index < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied):
                continue
            match = (index, end, entity_type)
            break
        if match is None:
            index += 1
            continue
        start, end, entity_type = match
        completed.append(
            {
                "id": "",
                "start": start,
                "end": end,
                "text": " ".join(tokens[start:end]),
                "type": entity_type,
            }
        )
        occupied.add((start, end))
        index = end
    return merge_fire_entities(completed)


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
                "head_id": str(head["id"]),
                "head_start": int(head["start"]),
                "head_end": int(head["end"]),
                "head_type": str(head["type"]),
                "tail_id": str(tail["id"]),
                "tail_start": int(tail["start"]),
                "tail_end": int(tail["end"]),
                "tail_type": str(tail["type"]),
            }
        )
    return normalized


def fire_entities_from_spans(
    rows: Any,
    tokens: list[str],
    allowed_types: set[str],
) -> list[dict[str, Any]]:
    indexed_rows = [
        {
            "start": row.get("start"),
            "end": row.get("end"),
            "type": row.get("type"),
        }
        for row in rows
        if isinstance(row, dict)
    ] if isinstance(rows, list) else []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for row in indexed_rows:
        try:
            start = int(row["start"])
            end = int(row["end"])
        except (TypeError, ValueError):
            continue
        entity_type = str(row.get("type") or "").strip()
        key = (start, end, entity_type)
        if entity_type not in allowed_types or not (0 <= start < end <= len(tokens)) or key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "id": "",
                "start": start,
                "end": end,
                "text": " ".join(tokens[start:end]),
                "type": entity_type,
            }
        )
    normalized.sort(key=lambda item: (item["start"], item["end"], item["type"]))
    for index, entity in enumerate(normalized):
        entity["id"] = f"e{index}"
    return normalized


def merge_fire_entity_review(
    draft: list[dict[str, Any]],
    reviewed: list[dict[str, Any]],
    *,
    replace_types: set[str],
) -> list[dict[str, Any]]:
    combined = [
        dict(entity)
        for entity in draft
        if str(entity.get("type")) not in replace_types
    ] + [
        dict(entity)
        for entity in reviewed
        if str(entity.get("type")) in replace_types
    ]
    deduped: dict[tuple[int, int, str], dict[str, Any]] = {}
    for entity in combined:
        key = (int(entity["start"]), int(entity["end"]), str(entity["type"]))
        deduped[key] = entity
    merged = sorted(
        deduped.values(),
        key=lambda item: (int(item["start"]), int(item["end"]), str(item["type"])),
    )
    for index, entity in enumerate(merged):
        entity["id"] = f"e{index}"
    return merged


def merge_fire_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge independent NER passes without allowing one pass to rewrite another."""
    deduped: dict[tuple[int, int, str], dict[str, Any]] = {}
    for entity in entities:
        key = (int(entity["start"]), int(entity["end"]), str(entity["type"]))
        deduped[key] = dict(entity)
    merged = sorted(
        deduped.values(),
        key=lambda item: (int(item["start"]), int(item["end"]), str(item["type"])),
    )
    for index, entity in enumerate(merged):
        entity["id"] = f"e{index}"
    return merged


def fire_entity_key(entity: dict[str, Any]) -> tuple[int, int, str]:
    return (int(entity["start"]), int(entity["end"]), str(entity["type"]))


def intersect_fire_entities(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep exact span/type agreements from independent extraction strategies."""
    right_keys = {
        (int(entity["start"]), int(entity["end"]), str(entity["type"]))
        for entity in right
    }
    agreed = [
        dict(entity)
        for entity in left
        if (int(entity["start"]), int(entity["end"]), str(entity["type"])) in right_keys
    ]
    return merge_fire_entities(agreed)


def mark_fire_entities(tokens: list[str], entities: list[dict[str, Any]]) -> str:
    openings: dict[int, list[dict[str, Any]]] = defaultdict(list)
    closings: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for entity in entities:
        openings[int(entity["start"])].append(entity)
        closings[int(entity["end"])].append(entity)
    rendered: list[str] = []
    for index, token in enumerate(tokens):
        for entity in sorted(openings.get(index, []), key=lambda row: int(row["end"]), reverse=True):
            rendered.append(f"<entity id=\"{entity['id']}\" type=\"{entity['type']}\">")
        rendered.append(token)
        for entity in sorted(closings.get(index + 1, []), key=lambda row: int(row["start"]), reverse=True):
            rendered.append("</entity>")
    return " ".join(rendered)


def build_fire_candidate_pairs(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for head in entities:
        for tail in entities:
            if head["id"] == tail["id"]:
                continue
            allowed = sorted(
                relation_type
                for relation_type, signatures in FIRE_RELATION_SIGNATURES.items()
                if (str(head["type"]), str(tail["type"])) in signatures
            )
            if allowed:
                candidates.append(
                    {
                        "head_id": str(head["id"]),
                        "tail_id": str(tail["id"]),
                        "allowed_types": allowed,
                    }
                )
    return candidates


def fire_entities_from_relation_candidates(
    rows: Any,
    tokens: list[str],
    allowed_entity_types: set[str],
    allowed_relation_types: set[str],
) -> list[dict[str, Any]]:
    """Recover only endpoints from structurally valid relation proposals."""
    endpoint_rows: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        relation_type = str(row.get("type") or "").strip()
        head_type = str(row.get("head_type") or "").strip()
        tail_type = str(row.get("tail_type") or "").strip()
        if relation_type not in allowed_relation_types:
            continue
        if (head_type, tail_type) not in FIRE_RELATION_SIGNATURES.get(relation_type, set()):
            continue
        endpoint_rows.extend(
            [
                {"text": str(row.get("head_text") or ""), "type": head_type},
                {"text": str(row.get("tail_text") or ""), "type": tail_type},
            ]
        )
    return align_fire_text_entities(endpoint_rows, tokens, allowed_entity_types)


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


def build_fire_ner_prompt(
    text: str,
    entity_types: tuple[str, ...],
    examples: str,
    *,
    focus: str = "all entity categories",
) -> str:
    allowed = ", ".join(entity_types)
    return f"""Extract every FIRE benchmark entity mention as an exact source span.

This pass focuses only on {focus}. Ignore categories outside the allowed list.
Return JSON only:
{{"entities":[{{"text":"exact source span","type":"Company"}}]}}
Allowed entity types: {allowed}

Rules:
- Copy each `text` exactly from the source. Use the complete noun phrase and do not paraphrase.
- Company is a named commercial or legal organization; Person is a named individual.
- Action is the explicit transaction/event word, not every ordinary verb.
- Product is a named or described good/service offered by a company; keep its full descriptive phrase.
- FinancialEntity is a financial measure, account, security, asset, liability, revenue/cost item, or investment.
- Designation is a person's job role or a company's contractual/competitive role.
- Money is a currency amount; Quantity is a percentage, count, multiple, or non-currency measure.
- Date includes explicit dates and named reporting periods.
- Sector is the industry phrase itself; BusinessUnit is an internal organizational division.
- Include repeated mentions separately. Do not invent implicit entities or label pronouns.

TEXT:
{text}

TRAINING EXAMPLES (only allowed categories are shown):
{examples}"""


def build_fire_indexed_ner_prompt(
    tokens: list[str],
    entity_types: tuple[str, ...],
    examples: str,
) -> str:
    numbered_text = " ".join(f"[{index}] {token}" for index, token in enumerate(tokens))
    allowed = ", ".join(entity_types)
    return f"""Extract every FIRE benchmark entity by its exact token interval.

Return JSON only:
{{"entities":[{{"start":0,"end":2,"type":"Company"}}]}}
`start` is the first included token index. `end` is the first excluded token index.
Allowed entity types: {allowed}

Rules:
- Read the bracketed token indexes; never count characters or invent indexes.
- Select the complete noun phrase, including all descriptive words belonging to the mention.
- Company is a named commercial/legal organization. Person is a named individual.
- Action is the explicit financial/corporate transaction event word, not an ordinary verb.
- Product is a named or described good/service offered by a company.
- FinancialEntity is a financial measure, account, security, asset, liability, revenue/cost item, or investment.
- Designation is a person's job role or a company's contractual/competitive role.
- Money is a currency amount. Quantity is a percentage, count, multiple, or non-currency measure.
- Date includes explicit dates and named reporting periods.
- Sector is the industry phrase itself. BusinessUnit is an internal organizational division.
- Include repeated mentions as separate intervals. Do not label pronouns or implicit entities.

INDEXED TEXT:
{numbered_text}

TRAINING EXAMPLES (offsets use the same end-exclusive convention):
{examples}"""


def render_fire_ner_examples(
    examples: Any,
    *,
    allowed_types: set[str] | None = None,
    include_offsets: bool = False,
) -> str:
    if not isinstance(examples, list) or not examples:
        return "(none)"
    blocks = []
    for example in examples:
        if not isinstance(example, dict):
            continue
        entities = []
        for entity in example.get("entities", []):
            if not isinstance(entity, dict) or (
                allowed_types is not None
                and str(entity.get("type")) not in allowed_types
            ):
                continue
            rendered = {"text": entity.get("text"), "type": entity.get("type")}
            if include_offsets:
                rendered = {
                    "start": entity.get("start"),
                    "end": entity.get("end"),
                    **rendered,
                }
            entities.append(rendered)
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


def render_fire_text_relation_examples(examples: Any) -> str:
    if not isinstance(examples, list) or not examples:
        return "(none)"
    blocks: list[str] = []
    for example in examples:
        if not isinstance(example, dict):
            continue
        entities_by_id = {
            str(entity.get("id")): entity
            for entity in example.get("entities", [])
            if isinstance(entity, dict)
        }
        relations: list[dict[str, str]] = []
        for relation in example.get("relations", []):
            if not isinstance(relation, dict):
                continue
            head = entities_by_id.get(str(relation.get("head_id") or ""))
            tail = entities_by_id.get(str(relation.get("tail_id") or ""))
            if head is None or tail is None:
                continue
            relations.append(
                {
                    "head_text": str(head.get("text") or ""),
                    "head_type": str(head.get("type") or ""),
                    "type": str(relation.get("type") or ""),
                    "tail_text": str(tail.get("text") or ""),
                    "tail_type": str(tail.get("type") or ""),
                }
            )
        blocks.append(
            f"TEXT: {example.get('text', '')}\nOUTPUT: "
            + json.dumps({"relations": relations}, ensure_ascii=False)
        )
    return "\n\n".join(blocks) or "(none)"
