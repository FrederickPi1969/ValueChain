from __future__ import annotations

import ast
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from valuechain.financial_ie.json_utils import parse_json_payload
from valuechain.financial_ie.models import BenchmarkCase


def score_prediction(case: BenchmarkCase, content: str) -> dict[str, Any]:
    if case.task == "finben_ner":
        return score_entities(case.gold, parse_entities(content))
    if case.task == "fire_joint_re":
        return score_fire(case.gold, content)
    if case.task == "finben_fnxl":
        return score_fnxl(case.gold, content)
    if case.task in {"finqa", "financebench"}:
        return score_answer(case, content)
    raise ValueError(f"Unsupported task: {case.task}")


def score_entities(gold: list[dict[str, str]], predicted: list[dict[str, str]]) -> dict[str, Any]:
    gold_items = multiset_items(gold, entity_key)
    predicted_items = multiset_items(predicted, entity_key)
    tp = sum(min(gold_items.get(key, 0), predicted_items.get(key, 0)) for key in gold_items)
    total_gold = sum(gold_items.values())
    total_predicted = sum(predicted_items.values())
    return prf(tp, total_predicted - tp, total_gold - tp)


def score_fire(gold: dict[str, Any], content: str) -> dict[str, Any]:
    try:
        payload = parse_json_payload(content)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    predicted_entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
    predicted_relations = payload.get("relations") if isinstance(payload.get("relations"), list) else []
    entity_metrics = score_entities(gold["entities"], predicted_entities)
    gold_relations = {relation_key(item) for item in gold["relations"]}
    pred_relations = {
        relation_key(item) for item in predicted_relations if isinstance(item, dict)
    }
    tp = len(gold_relations & pred_relations)
    relation_metrics = prf(tp, len(pred_relations - gold_relations), len(gold_relations - pred_relations))
    return {
        **{f"entity_{key}": value for key, value in entity_metrics.items()},
        **{f"relation_{key}": value for key, value in relation_metrics.items()},
    }


def score_fnxl(gold: dict[str, Any], content: str) -> dict[str, Any]:
    tokens = [str(token) for token in gold["tokens"]]
    gold_labels = [normalize_concept_label(str(label)) for label in gold["labels"]]
    predicted = parse_token_labels(content, tokens)
    predicted_labels = [normalize_concept_label(predicted.get(index, "O")) for index in range(len(tokens))]
    gold_positive = {(index, label) for index, label in enumerate(gold_labels) if label != "O"}
    pred_positive = {(index, label) for index, label in enumerate(predicted_labels) if label != "O"}
    tp = len(gold_positive & pred_positive)
    metrics = prf(tp, len(pred_positive - gold_positive), len(gold_positive - pred_positive))
    metrics["token_accuracy"] = round(
        sum(left == right for left, right in zip(gold_labels, predicted_labels, strict=True)) / max(1, len(tokens)),
        6,
    )
    return metrics


def score_answer(case: BenchmarkCase, content: str) -> dict[str, Any]:
    payload: Any = None
    try:
        payload = parse_json_payload(content)
    except ValueError:
        pass
    answer = payload.get("answer") if isinstance(payload, dict) else content.strip()
    correct = answers_equivalent(str(case.gold), answer, financebench=case.task == "financebench")
    result: dict[str, Any] = {
        "answer_correct": int(correct),
        "predicted_answer": answer,
        "tool_answer": None,
        "tool_answer_correct": 0,
        "expression_valid": 0,
        "harness_answer_correct": int(correct),
    }
    if isinstance(payload, dict):
        tool_answer = evaluate_expression(str(payload.get("expression") or ""))
        result["tool_answer"] = tool_answer
        tool_answer_for_scoring: Any = tool_answer
        if tool_answer is not None and ("%" in str(case.gold) or "%" in str(answer)):
            tool_answer_for_scoring = f"{tool_answer}%"
        tool_correct = int(
            answers_equivalent(
                str(case.gold),
                tool_answer_for_scoring,
                financebench=case.task == "financebench",
            )
        )
        result["tool_answer_correct"] = tool_correct
        result["expression_valid"] = int(tool_answer is not None)
        result["harness_answer_correct"] = tool_correct if tool_answer is not None else int(correct)
    if case.task == "financebench" and "cited_pages" in (payload if isinstance(payload, dict) else {}):
        cited_pages = parse_ints(payload.get("cited_pages", [])) if isinstance(payload, dict) else set()
        gold_pages = {int(page) for page in case.metadata.get("evidence_pages", [])}
        result["citation_page_hit"] = int(bool(cited_pages & gold_pages))
        result["cited_pages"] = sorted(cited_pages)
    return result


def parse_entities(content: str) -> list[dict[str, str]]:
    try:
        payload = parse_json_payload(content)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("entities"), list):
        return [item for item in payload["entities"] if isinstance(item, dict)]
    entities: list[dict[str, str]] = []
    for line in content.splitlines():
        clean = line.strip(" -*\t")
        if "," not in clean:
            continue
        text, entity_type = clean.rsplit(",", 1)
        if entity_type.strip().upper() in {"PER", "ORG", "LOC"}:
            entities.append({"text": text.strip(), "type": entity_type.strip()})
    return entities


def parse_token_labels(content: str, tokens: list[str]) -> dict[int, str]:
    try:
        payload = parse_json_payload(content)
    except ValueError:
        payload = None
    labels: dict[int, str] = {}
    if isinstance(payload, dict) and isinstance(payload.get("labels"), list):
        for item in payload["labels"]:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("token_index"))
            except (TypeError, ValueError):
                continue
            labels[index] = str(item.get("concept") or "O")
        return labels
    cursor = 0
    for line in content.splitlines():
        if ":" not in line or cursor >= len(tokens):
            continue
        token, label = line.rsplit(":", 1)
        while cursor < len(tokens) and tokens[cursor] != token.strip():
            cursor += 1
        if cursor < len(tokens):
            labels[cursor] = label.strip()
            cursor += 1
    return labels


def answers_equivalent(gold: str, predicted: Any, *, financebench: bool = False) -> bool:
    if predicted is None:
        return False
    gold_number = parse_number(gold)
    predicted_number = parse_number(str(predicted))
    if gold_number is not None and predicted_number is not None:
        tolerance = max(Decimal("0.001"), abs(gold_number) * Decimal("0.00001"))
        if financebench:
            if "%" in gold:
                tolerance = Decimal("0.0005")
            else:
                tolerance = max(tolerance, Decimal("0.005"))
            if "%" not in gold and re.search(r"\.00\D*$", gold.strip()) and abs(gold_number) >= 100:
                tolerance = max(tolerance, Decimal("0.5"))
        return abs(gold_number - predicted_number) <= tolerance
    normalized_gold = normalize_answer(gold)
    normalized_predicted = normalize_answer(str(predicted))
    boolean_aliases = {"yes": "yes", "true": "yes", "no": "no", "false": "no"}
    if normalized_gold in boolean_aliases and normalized_predicted in boolean_aliases:
        return boolean_aliases[normalized_gold] == boolean_aliases[normalized_predicted]
    if normalized_gold == normalized_predicted:
        return True
    return token_f1(normalized_gold, normalized_predicted) >= 0.72


def parse_number(value: str) -> Decimal | None:
    text = value.strip().lower().replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = Decimal(match.group())
    except InvalidOperation:
        return None
    if negative:
        number = -abs(number)
    if "%" in text:
        number /= Decimal("100")
    return number


def normalize_answer(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def validate_arithmetic(payload: dict[str, Any]) -> bool:
    operation = str(payload.get("operation") or "").lower().strip()
    operands = payload.get("operands")
    answer = parse_number(str(payload.get("answer")))
    if not operation or not isinstance(operands, list) or answer is None:
        return False
    try:
        values = [Decimal(str(value)) for value in operands]
    except (InvalidOperation, ValueError):
        return False
    if len(values) < 2:
        return False
    expected: Decimal
    if operation in {"add", "sum", "addition"}:
        expected = sum(values)
    elif operation in {"subtract", "difference", "subtraction"}:
        expected = values[0] - values[1]
    elif operation in {"multiply", "multiplication", "product"}:
        expected = math.prod(values)
    elif operation in {"divide", "division", "ratio"} and values[1] != 0:
        expected = values[0] / values[1]
    elif operation in {"percent_change", "percentage_change"} and values[1] != 0:
        expected = (values[0] - values[1]) / abs(values[1]) * 100
    else:
        return False
    tolerance = max(Decimal("0.02"), abs(expected) * Decimal("0.002"))
    return abs(expected - answer) <= tolerance


def evaluate_expression(expression: str) -> str | float | bool | None:
    if not expression.strip() or len(expression) > 300:
        return None
    expression = expression.replace("^", "**")
    try:
        node = ast.parse(expression, mode="eval")
        value = _eval_node(node.body)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
        return None
    if isinstance(value, bool):
        return value
    if not math.isfinite(float(value)):
        return None
    return round(float(value), 8)


def _eval_node(node: ast.AST) -> float | bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = float(_eval_node(node.operand))
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
        left = float(_eval_node(node.left))
        right = float(_eval_node(node.right))
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Pow):
            if abs(right) > 10 or abs(left) > 1e15:
                raise ValueError("Unsafe exponent")
            return left**right
        return left / right
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = float(_eval_node(node.left))
        right = float(_eval_node(node.comparators[0]))
        operation = node.ops[0]
        if isinstance(operation, ast.Gt):
            return left > right
        if isinstance(operation, ast.Lt):
            return left < right
        if isinstance(operation, ast.GtE):
            return left >= right
        if isinstance(operation, ast.LtE):
            return left <= right
    raise ValueError("Unsupported expression")


def token_f1(left: str, right: str) -> float:
    left_tokens = left.split()
    right_tokens = right.split()
    if not left_tokens or not right_tokens:
        return 0.0
    left_counts: dict[str, int] = {}
    right_counts: dict[str, int] = {}
    for token in left_tokens:
        left_counts[token] = left_counts.get(token, 0) + 1
    for token in right_tokens:
        right_counts[token] = right_counts.get(token, 0) + 1
    common = sum(min(count, right_counts.get(token, 0)) for token, count in left_counts.items())
    precision = common / len(right_tokens)
    recall = common / len(left_tokens)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def parse_ints(value: Any) -> set[int]:
    if isinstance(value, (int, float, str)):
        value = [value]
    if not isinstance(value, list):
        return set()
    parsed: set[int] = set()
    for item in value:
        match = re.search(r"\d+", str(item))
        if match:
            parsed.add(int(match.group()))
    return parsed


def entity_key(item: dict[str, Any]) -> tuple[str, str]:
    return normalize_span(str(item.get("text") or "")), str(item.get("type") or "").upper().strip()


def relation_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_span(str(item.get("head") or "")),
        normalize_span(str(item.get("tail") or "")),
        str(item.get("type") or "").lower().strip(),
    )


def normalize_span(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,:;\"'").lower()


def normalize_concept_label(value: str) -> str:
    clean = value.strip()
    if clean == "O" or not clean:
        return "O"
    return clean.removeprefix("B-").removeprefix("I-")


def multiset_items(items: list[dict[str, Any]], key_fn: Any) -> dict[Any, int]:
    counts: dict[Any, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = key_fn(item)
        if not key[0] or not key[1]:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }
