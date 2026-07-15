from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from valuechain.financial_ie.models import BenchmarkCase


def load_finben_ner(path: Path, *, limit: int = 30, seed: int = 1969) -> list[BenchmarkCase]:
    rows = read_parquet_records(path)
    selected = deterministic_sample(rows, limit, seed)
    return [
        BenchmarkCase(
            case_id=f"finben_ner:{index}",
            task="finben_ner",
            source="FinBen/flare-ner",
            text=str(row["text"]),
            gold=parse_ner_answer(str(row["answer"])),
        )
        for index, row in enumerate(selected)
    ]


def load_finben_fnxl(path: Path, *, limit: int = 20, seed: int = 1969) -> list[BenchmarkCase]:
    rows = read_parquet_records(path)
    label_catalog = sorted(
        {
            str(label).removeprefix("B-").removeprefix("I-")
            for row in rows
            for label in row["label"]
            if label != "O"
        }
    )
    selected = deterministic_sample(rows, limit, seed)
    cases: list[BenchmarkCase] = []
    for row in selected:
        text = str(row["text"])
        ranked_candidates = rank_concept_labels(text, label_catalog, limit=len(label_catalog))
        cases.append(
            BenchmarkCase(
                case_id=f"finben_fnxl:{row['id']}",
                task="finben_fnxl",
                source="FinBen/flare-fnxl",
                text=text,
                gold={"tokens": list(row["token"]), "labels": list(row["label"])},
                metadata={
                    "candidate_labels": ranked_candidates,
                    "label_catalog_size": len(label_catalog),
                    "gold_in_top_30": any(
                        normalize_bio_label(label) in ranked_candidates[:30]
                        for label in row["label"]
                        if label != "O"
                    ),
                },
            )
        )
    return cases


def load_fire(
    data_path: Path,
    types_path: Path,
    *,
    limit: int = 30,
    seed: int = 1969,
) -> list[BenchmarkCase]:
    rows = json.loads(data_path.read_text(encoding="utf-8"))
    type_catalog = json.loads(types_path.read_text(encoding="utf-8"))
    selected = deterministic_sample(rows, limit, seed)
    cases: list[BenchmarkCase] = []
    for row in selected:
        entities = [
            {"text": entity["text"], "type": entity["type"]}
            for entity in row["entities"]
        ]
        relations = [
            {
                "head": row["entities"][relation["head"]]["text"],
                "tail": row["entities"][relation["tail"]]["text"],
                "type": relation["type"],
            }
            for relation in row["relations"]
        ]
        cases.append(
            BenchmarkCase(
                case_id=f"fire:{row['orig_id']}",
                task="fire_joint_re",
                source="FIRE/NAACL-2024",
                text=" ".join(row["tokens"]),
                gold={"entities": entities, "relations": relations},
                metadata={
                    "entity_types": sorted(type_catalog["entities"]),
                    "relation_types": sorted(type_catalog["relations"]),
                },
            )
        )
    return cases


def load_finqa(path: Path, *, limit: int = 30, seed: int = 1969) -> list[BenchmarkCase]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        operation = str(row.get("qa", {}).get("program", "")).split("(", 1)[0]
        grouped[operation].append(row)
    selected = round_robin_sample(grouped, limit, seed)
    cases: list[BenchmarkCase] = []
    for row in selected:
        qa = row["qa"]
        context = render_finqa_context(row)
        cases.append(
            BenchmarkCase(
                case_id=f"finqa:{row['id']}",
                task="finqa",
                source="FinQA/EMNLP-2021",
                text=context,
                question=str(qa["question"]),
                gold=qa.get("exe_ans", qa["answer"]),
                metadata={
                    "display_answer": qa.get("answer", ""),
                    "program": qa.get("program", ""),
                    "gold_evidence": qa.get("gold_inds", {}),
                    "filename": row.get("filename", ""),
                },
            )
        )
    return cases


def load_financebench(
    questions_path: Path,
    pdf_dir: Path,
    *,
    limit: int = 40,
    seed: int = 1969,
    metrics_only: bool = True,
) -> list[BenchmarkCase]:
    rows = [json.loads(line) for line in questions_path.read_text(encoding="utf-8").splitlines()]
    if metrics_only:
        rows = [row for row in rows if row.get("question_type") == "metrics-generated"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("question_reasoning") or "unspecified")].append(row)
    selected = round_robin_sample(grouped, limit, seed)
    cases: list[BenchmarkCase] = []
    for row in selected:
        evidence = row.get("evidence") or []
        cases.append(
            BenchmarkCase(
                case_id=f"financebench:{row['financebench_id']}",
                task="financebench",
                source="FinanceBench",
                text="\n\n".join(str(item.get("evidence_text") or "") for item in evidence),
                question=str(row["question"]),
                gold=str(row["answer"]),
                metadata={
                    "company": row.get("company", ""),
                    "doc_name": row["doc_name"],
                    "pdf_path": str(pdf_dir / f"{row['doc_name']}.pdf"),
                    "reasoning": row.get("question_reasoning"),
                    "question_type": row.get("question_type"),
                    "evidence_pages": sorted(
                        {int(item["evidence_page_num"]) + 1 for item in evidence if item.get("evidence_page_num") is not None}
                    ),
                },
            )
        )
    return cases


def read_parquet_records(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("Parquet benchmark preparation requires pandas plus pyarrow") from exc
    return pd.read_parquet(path).to_dict(orient="records")


def deterministic_sample(rows: list[dict[str, Any]], limit: int, seed: int) -> list[dict[str, Any]]:
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    return shuffled[: min(limit, len(shuffled))]


def round_robin_sample(
    groups: dict[str, list[dict[str, Any]]],
    limit: int,
    seed: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    queues = {key: list(rows) for key, rows in groups.items()}
    for rows in queues.values():
        rng.shuffle(rows)
    keys = sorted(queues)
    selected: list[dict[str, Any]] = []
    while len(selected) < limit and any(queues.values()):
        for key in keys:
            if queues[key] and len(selected) < limit:
                selected.append(queues[key].pop())
    return selected


def parse_ner_answer(answer: str) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    for line in answer.splitlines():
        if "," not in line:
            continue
        text, entity_type = line.rsplit(",", 1)
        entities.append({"text": text.strip(), "type": entity_type.strip()})
    return entities


def render_finqa_context(row: dict[str, Any]) -> str:
    sections: list[str] = []
    if row.get("pre_text"):
        sections.append("TEXT BEFORE TABLE:\n" + "\n".join(row["pre_text"]))
    table = row.get("table_ori") or row.get("table") or []
    if table:
        sections.append("TABLE:\n" + "\n".join(" | ".join(map(str, table_row)) for table_row in table))
    if row.get("post_text"):
        sections.append("TEXT AFTER TABLE:\n" + "\n".join(row["post_text"]))
    return "\n\n".join(sections)


def rank_concept_labels(text: str, labels: Iterable[str], *, limit: int) -> list[str]:
    text_tokens = set(re.findall(r"[a-z0-9]+", text.lower()))

    def score(label: str) -> tuple[float, str]:
        words = {word.lower() for word in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", label)}
        overlap = len(text_tokens & words)
        coverage = overlap / max(1, len(words))
        return overlap + coverage, label

    return [label for _, label in sorted((score(label) for label in labels), reverse=True)[:limit]]


def normalize_bio_label(value: str) -> str:
    return value.removeprefix("B-").removeprefix("I-")
