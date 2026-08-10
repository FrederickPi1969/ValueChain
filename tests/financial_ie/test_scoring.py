from valuechain.financial_ie.models import BenchmarkCase
from valuechain.financial_ie.scoring import (
    answers_equivalent,
    evaluate_expression,
    score_prediction,
    validate_arithmetic,
)


def test_ner_scoring_counts_duplicate_mentions() -> None:
    case = BenchmarkCase(
        "1",
        "finben_ner",
        "test",
        "Acme and Acme",
        gold=[{"text": "Acme", "type": "ORG"}, {"text": "Acme", "type": "ORG"}],
    )
    score = score_prediction(case, '{"entities":[{"text":"Acme","type":"ORG"}]}')
    assert score["tp"] == 1
    assert score["fn"] == 1
    assert score["recall"] == 0.5


def test_financial_answer_normalization_handles_currency_and_parentheses() -> None:
    assert answers_equivalent("$1,577.00", "1577")
    assert answers_equivalent("-32", "(32)")


def test_arithmetic_validator_checks_operands() -> None:
    assert validate_arithmetic({"operation": "subtract", "operands": [5829, 5735], "answer": "94"})
    assert not validate_arithmetic({"operation": "subtract", "operands": [5829, 5735], "answer": "95"})


def test_fire_scoring_preserves_relation_direction() -> None:
    case = BenchmarkCase(
        "1",
        "fire_joint_re",
        "test",
        "A owns B",
        gold={
            "entities": [{"text": "A", "type": "Company"}, {"text": "B", "type": "Company"}],
            "relations": [{"head": "A", "tail": "B", "type": "Subsidiaryof"}],
        },
    )
    prediction = (
        '{"entities":[{"text":"A","type":"Company"},{"text":"B","type":"Company"}],'
        '"relations":[{"head":"B","tail":"A","type":"Subsidiaryof"}]}'
    )
    score = score_prediction(case, prediction)
    assert score["relation_tp"] == 0
    assert score["relation_fn"] == 1


def test_financebench_percentage_tolerance_respects_reported_precision() -> None:
    assert answers_equivalent("16.5%", "16.52%", financebench=True)
    assert not answers_equivalent("0.4%", "0.9%", financebench=True)


def test_expression_evaluator_supports_bounded_financial_cagr() -> None:
    assert evaluate_expression("(65984 / 65398) ^ (1 / 2) - 1") == 0.00447027


def test_answer_score_uses_calculator_result_for_harness_metric() -> None:
    case = BenchmarkCase("1", "financebench", "test", "", question="margin", gold="16.5%")
    score = score_prediction(case, '{"answer":"15.9%","expression":"(11512+2763)/86392*100"}')
    assert score["answer_correct"] == 0
    assert score["tool_answer_correct"] == 1
    assert score["harness_answer_correct"] == 1


def test_answer_score_counts_non_json_output_in_harness_denominator() -> None:
    case = BenchmarkCase("1", "financebench", "test", "", question="value", gold="10")
    score = score_prediction(case, "not available")
    assert score["expression_valid"] == 0
    assert score["harness_answer_correct"] == 0
