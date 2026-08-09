from valuechain.financial_ie.benchmark import (
    align_fire_text_entities,
    normalize_fire_id_relations,
    rescore_rows,
    summarize_results,
)


def test_summarize_results_groups_tasks_and_errors() -> None:
    summary = summarize_results(
        [
            {"task": "ner", "scores": {"f1": 1.0}, "retrieval": {}, "latency_s": 2, "error": ""},
            {"task": "ner", "scores": {"f1": 0.0}, "retrieval": {}, "latency_s": 4, "error": "bad"},
        ]
    )
    task = summary["tasks"]["ner"]
    assert task["count"] == 2
    assert task["errors"] == 1
    assert task["avg_latency_s"] == 3
    assert task["metrics"]["f1"] == 0.5
    assert task["metric_counts"]["f1"] == 2


def test_summarize_results_does_not_average_answer_values() -> None:
    summary = summarize_results(
        [
            {
                "task": "finqa",
                "scores": {
                    "answer_correct": 1,
                    "predicted_answer": 123.4,
                    "tool_answer": 123.4,
                },
                "retrieval": {},
                "latency_s": 1,
                "error": "",
            }
        ]
    )

    assert summary["tasks"]["finqa"]["metrics"] == {"answer_correct": 1.0}


def test_rescore_rows_updates_stale_scores_without_model_call() -> None:
    rows = [
        {
            "case_id": "financebench:1",
            "task": "financebench",
            "source": "FinanceBench",
            "input_text": "Revenue was 10.",
            "question": "What was revenue?",
            "gold": "10",
            "metadata": {},
            "prediction": '{"answer":"10","expression":null}',
            "scores": {"answer_correct": 0},
            "retrieved_chunks": [],
            "error": "",
        }
    ]

    rescored = rescore_rows(rows)

    assert rescored[0]["scores"]["answer_correct"] == 1
    assert rescored[0]["scorer_version"] == "financial-ie-scorer-v0.2"


def test_normalize_fire_id_relations_enforces_direction_and_type_signature() -> None:
    entities = [
        {"id": "e0", "text": "cloud services", "type": "Product"},
        {"id": "e1", "text": "Acme", "type": "Company"},
    ]
    relations = normalize_fire_id_relations(
        [
            {"head_id": "e0", "tail_id": "e1", "type": "Productof"},
            {"head_id": "e1", "tail_id": "e0", "type": "Productof"},
            {"head_id": "missing", "tail_id": "e1", "type": "Productof"},
        ],
        entities,
        {"Productof"},
    )

    assert relations == [{"head": "cloud services", "tail": "Acme", "type": "Productof"}]


def test_align_fire_text_entities_handles_repeated_mentions() -> None:
    entities = align_fire_text_entities(
        [
            {"text": "Acme", "type": "Company"},
            {"text": "Acme", "type": "Company"},
            {"text": "invented company", "type": "Company"},
        ],
        ["Acme", "bought", "Acme", "."],
        {"Company"},
    )

    assert [(entity["id"], entity["start"], entity["end"]) for entity in entities] == [
        ("e0", 0, 1),
        ("e1", 2, 3),
    ]
