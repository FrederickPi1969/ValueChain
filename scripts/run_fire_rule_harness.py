#!/usr/bin/env python3
"""Evaluate train-derived lexical relation rules against saved FIRE predictions."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from valuechain.financial_ie.benchmark import FIRE_RELATION_SIGNATURES


RelationKey = tuple[int, int, str, str, int, int, str]
RuleStats = dict[str, dict[str, list[int]]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-data", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--min-support", type=int, default=20)
    parser.add_argument("--min-precision", type=float, default=0.85)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def normalize_token(token: Any) -> str:
    return re.sub(r"[^a-z0-9$%]+", "", str(token).casefold())


def relation_features(
    tokens: list[Any],
    head: dict[str, Any],
    tail: dict[str, Any],
) -> set[str]:
    head_start, head_end = int(head["start"]), int(head["end"])
    tail_start, tail_end = int(tail["start"]), int(tail["end"])
    low, high = min(head_end, tail_end), max(head_start, tail_start)
    between = [normalize_token(token) for token in tokens[low:high]]
    between = [token for token in between if token]
    sentence = [normalize_token(token) for token in tokens]
    sentence = [token for token in sentence if token]
    order = "head_before_tail" if head_start < tail_start else "tail_before_head"
    distance = min(6, abs(tail_start - head_start) // 3)
    features = {
        f"order={order}|distance={distance}",
        f"order={order}|exact_between={'_'.join(between[:12])}",
    }
    for length in (1, 2, 3):
        if len(between) >= length:
            features.add(f"order={order}|prefix={length}:{'_'.join(between[:length])}")
            features.add(f"order={order}|suffix={length}:{'_'.join(between[-length:])}")
        for values, scope in ((between, "between"), (sentence, "sentence")):
            for index in range(len(values) - length + 1):
                features.add(
                    f"order={order}|{scope}={length}:{'_'.join(values[index:index + length])}"
                )
    return features


def mine_rules(rows: list[dict[str, Any]]) -> RuleStats:
    rules: RuleStats = {}
    for relation_type, signatures in FIRE_RELATION_SIGNATURES.items():
        counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for row in rows:
            entities = row.get("entities", [])
            gold = {
                (int(relation["head"]), int(relation["tail"]), str(relation["type"]))
                for relation in row.get("relations", [])
            }
            for head_index, head in enumerate(entities):
                for tail_index, tail in enumerate(entities):
                    if head_index == tail_index:
                        continue
                    if (str(head["type"]), str(tail["type"])) not in signatures:
                        continue
                    positive = (head_index, tail_index, relation_type) in gold
                    for feature in relation_features(row["tokens"], head, tail):
                        counts[feature][0] += int(positive)
                        counts[feature][1] += 1
        rules[relation_type] = dict(counts)
    return rules


def strict_relation_key(row: dict[str, Any]) -> RelationKey:
    return (
        int(row["head_start"]),
        int(row["head_end"]),
        str(row["head_type"]),
        str(row["type"]),
        int(row["tail_start"]),
        int(row["tail_end"]),
        str(row["tail_type"]),
    )


def rule_predictions(
    row: dict[str, Any],
    rules: RuleStats,
    *,
    min_support: int,
    min_precision: float,
) -> set[RelationKey]:
    entities = row["intermediate_predictions"]["normalized_entities"]
    tokens = row["metadata"]["tokens"]
    predictions: set[RelationKey] = set()
    for relation_type, signatures in FIRE_RELATION_SIGNATURES.items():
        relation_rules = rules[relation_type]
        for head in entities:
            for tail in entities:
                if head["id"] == tail["id"]:
                    continue
                if (str(head["type"]), str(tail["type"])) not in signatures:
                    continue
                candidates: list[tuple[float, int, int]] = []
                for feature in relation_features(tokens, head, tail):
                    positive, total = relation_rules.get(feature, (0, 0))
                    if positive:
                        candidates.append((positive / total, positive, total))
                best = max(candidates, key=lambda item: (item[0], item[1], -item[2])) if candidates else None
                if best and best[0] >= min_precision and best[1] >= min_support:
                    predictions.add(
                        (
                            int(head["start"]),
                            int(head["end"]),
                            str(head["type"]),
                            relation_type,
                            int(tail["start"]),
                            int(tail["end"]),
                            str(tail["type"]),
                        )
                    )
    return predictions


def metrics(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def evaluate(rows: list[dict[str, Any]], rules: RuleStats, args: argparse.Namespace) -> dict[str, Any]:
    totals = {name: Counter() for name in ("llm", "rules", "union")}
    endpoint_errors = Counter()
    for row in rows:
        gold = {strict_relation_key(relation) for relation in row["gold"]["relations"]}
        llm = {
            strict_relation_key(relation)
            for relation in json.loads(row["prediction"]).get("relations", [])
        }
        inferred = rule_predictions(
            row,
            rules,
            min_support=args.min_support,
            min_precision=args.min_precision,
        )
        for name, predictions in (("llm", llm), ("rules", inferred), ("union", llm | inferred)):
            totals[name]["tp"] += len(predictions & gold)
            totals[name]["fp"] += len(predictions - gold)
            totals[name]["fn"] += len(gold - predictions)
        entities = {
            (int(entity["start"]), int(entity["end"]), str(entity["type"]))
            for entity in row["intermediate_predictions"]["normalized_entities"]
        }
        for relation in gold - llm:
            head_present = relation[:3] in entities
            tail_present = relation[4:] in entities
            endpoint_errors[
                "both_present"
                if head_present and tail_present
                else "both_missing"
                if not head_present and not tail_present
                else "head_missing"
                if not head_present
                else "tail_missing"
            ] += 1
    return {
        "configuration": {
            "min_support": args.min_support,
            "min_precision": args.min_precision,
            "rule_source": "FIRE train split only",
        },
        "llm": metrics(**totals["llm"]),
        "rules": metrics(**totals["rules"]),
        "union": metrics(**totals["union"]),
        "llm_false_negative_endpoint_taxonomy": dict(endpoint_errors),
    }


def main() -> None:
    args = parse_args()
    train_rows = json.loads(args.train_data.read_text(encoding="utf-8"))
    prediction_rows = [
        json.loads(line)
        for line in args.predictions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = evaluate(prediction_rows, mine_rules(train_rows), args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
