from __future__ import annotations

import json

from valuechain.financial_ie.models import BenchmarkCase, DocumentChunk


SYSTEM_DIRECT = "Follow the task exactly. Return only the requested answer without commentary."
SYSTEM_STRUCTURED = """You are a precise financial information extraction engine.
Use only the supplied text. Never fill missing facts from memory. Preserve entity spans, direction,
units, signs, periods, and relation arguments. Your first character must be { and your last character
must be }. Return exactly one valid JSON object without markdown or commentary."""


def build_benchmark_prompt(
    case: BenchmarkCase,
    *,
    style: str,
    retrieved_chunks: list[DocumentChunk] | None = None,
) -> tuple[str, str, int]:
    if style not in {"direct", "structured", "retrieval"}:
        raise ValueError(f"Unknown prompt style: {style}")
    if case.task == "finben_ner":
        return build_ner(case, style)
    if case.task == "finben_fnxl":
        return build_fnxl(case, style)
    if case.task == "fire_joint_re":
        return build_fire(case, style)
    if case.task == "finqa":
        return build_qa(case, style, case.text)
    if case.task == "financebench":
        context = render_chunks(retrieved_chunks) if retrieved_chunks is not None else case.text
        return build_qa(
            case,
            style,
            context,
            request_citations=retrieved_chunks is not None,
        )
    raise ValueError(f"Unsupported benchmark task: {case.task}")


def build_ner(case: BenchmarkCase, style: str) -> tuple[str, str, int]:
    if style == "direct":
        user = (
            "Identify every PER, ORG, and LOC named entity. Return one line per entity as "
            "entity text, TYPE. Preserve repeated mentions.\n\nText:\n" + case.text
        )
        return SYSTEM_DIRECT, user, 700
    user = (
        "Extract all named entity mentions, including repeated mentions. Types: PER, ORG, LOC.\n"
        "Schema: {\"entities\":[{\"text\":\"exact source span\",\"type\":\"PER|ORG|LOC\"}]}\n"
        "Follow the SEC-contract benchmark convention: defined party-role mentions such as Borrower and "
        "Lender are tagged PER even when they refer to a legal party. Do not tag dates as LOC.\n\n"
        f"TEXT:\n{case.text}"
    )
    return SYSTEM_STRUCTURED, user, 700


def build_fnxl(case: BenchmarkCase, style: str) -> tuple[str, str, int]:
    tokens = list(case.gold["tokens"])
    candidates = list(case.metadata["candidate_labels"])
    if style == "direct":
        labels = candidates
        output = "one token:label line for every input token"
        schema = output
        input_block = f"Tokens:\n{json.dumps(tokens, ensure_ascii=False)}"
    else:
        labels = candidates
        schema = '{"labels":[{"token_index":0,"concept":"ConceptName"}]}'
        numeric_candidates = [
            {"token_index": index, "token": token}
            for index, token in enumerate(tokens)
            if any(character.isdigit() for character in token)
        ]
        input_block = (
            "The tokenizer has already located every numeric candidate. Choose only from these exact indexes:\n"
            + json.dumps(numeric_candidates, ensure_ascii=False)
            + "\nFull token sequence for context:\n"
            + json.dumps(tokens, ensure_ascii=False)
        )
    user = (
        "Assign a financial concept only to numeric value tokens that the surrounding sentence explicitly defines. "
        "Indexes are zero-based. Never label $, commas, years, or unit words when the measured value is the adjacent "
        "number. Example: tokens [\"was\",\"$\",\"49\",\"million\"] => token_index 2. "
        "Unlisted numeric tokens are O. Candidate concepts:\n"
        + "\n".join(labels)
        + f"\n\n{input_block}\n\nReturn {schema}."
    )
    return SYSTEM_DIRECT if style == "direct" else SYSTEM_STRUCTURED, user, 1000


def build_fire(case: BenchmarkCase, style: str) -> tuple[str, str, int]:
    entity_types = ", ".join(case.metadata["entity_types"])
    relation_types = ", ".join(case.metadata["relation_types"])
    if style == "direct":
        user = (
            f"Extract entities with types [{entity_types}] and relations with types [{relation_types}]. "
            "Return JSON with entities and relations.\n\n" + case.text
        )
    else:
        user = f"""Extract exact-span entities and directed relations. This benchmark annotates not only names:
Action is the explicit transaction verb; Designation is a job/contract role; FinancialEntity is a financial
measure or instrument; Money includes currency amounts; Quantity includes percentages or counts; Sector is an
industry phrase. Include repeated entity mentions when separately annotated.
Entity types: {entity_types}
Relation types: {relation_types}
Schema:
{{"entities":[{{"text":"exact span","type":"type"}}],
 "relations":[{{"head":"exact entity span","tail":"exact entity span","type":"type"}}]}}
Only emit a relation when both endpoint entities appear in entities. Preserve head-to-tail direction.
Key directions: component -> aggregate for Constituentof; product -> company for Productof; person -> company
for Employeeof; financial item -> amount for Value/Quantity; amount -> date for Valuein; company -> sector for Sector.

Example: "Shares of Tesla dropped 14% over the last quarter" includes entities Shares/FinancialEntity,
Tesla/Company, 14%/Quantity, last quarter/Date; relations Shares->Tesla/Propertyof,
Shares->14%/ValueChangeDecreaseby, and 14%->last quarter/Valuein.

TEXT:
{case.text}"""
    return SYSTEM_DIRECT if style == "direct" else SYSTEM_STRUCTURED, user, 1200


def build_qa(
    case: BenchmarkCase,
    style: str,
    context: str,
    *,
    request_citations: bool = False,
) -> tuple[str, str, int]:
    if style == "direct":
        user = f"CONTEXT:\n{context}\n\nQUESTION:\n{case.question}\n\nANSWER:"
        return SYSTEM_DIRECT, user, 500
    citation_instruction = ""
    if request_citations:
        citation_instruction = ' Include "cited_chunk_ids" and "cited_pages" from the supplied chunk headers.'
    user = f"""Answer from the supplied financial evidence only. Preserve requested units and sign.
For arithmetic, provide a plain Python arithmetic expression using only source numbers and operators + - * / ( ).
For comparisons, use >, <, >=, or <=. If the question asks for a percentage, answer with a % sign.
If the necessary values appear anywhere in the context, compute the answer; do not abstain merely because the
layout is noisy. Use the denominator implied by phrases such as "compared with", "as a percentage of", or
"change from A to B". If evidence is genuinely insufficient, set answer to null.
Return {{"answer":string|null,"expression":string|null,"evidence_quote":string}}.{citation_instruction}

CONTEXT:
{context}

QUESTION:
{case.question}"""
    return SYSTEM_STRUCTURED, user, 700


def render_chunks(chunks: list[DocumentChunk] | None) -> str:
    if not chunks:
        return ""
    rendered: list[str] = []
    for chunk in chunks:
        header = f"[chunk_id={chunk.chunk_id}; page={chunk.page or 'unknown'}; section={chunk.section_hint or 'unknown'}]"
        rendered.append(f"{header}\n{chunk.text}")
    return "\n\n".join(rendered)
