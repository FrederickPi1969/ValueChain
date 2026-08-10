import json

from valuechain.financial_ie.datasets import (
    load_fire,
    parse_ner_answer,
    rank_concept_labels,
    render_finqa_context,
    retrieve_fire_examples,
)


def test_parse_ner_answer_preserves_duplicate_mentions() -> None:
    rows = parse_ner_answer("Acme Corp, ORG\nAcme Corp, ORG\nParis, LOC")
    assert len(rows) == 3
    assert rows[-1] == {"text": "Paris", "type": "LOC"}


def test_rank_concept_labels_uses_semantic_label_words() -> None:
    labels = ["InterestExpense", "InventoryWriteDown", "CashAndCashEquivalentsAtCarryingValue"]
    ranked = rank_concept_labels("The company recorded an inventory write-down of $4 million.", labels, limit=2)
    assert ranked[0] == "InventoryWriteDown"


def test_render_finqa_context_keeps_table_structure() -> None:
    rendered = render_finqa_context(
        {"pre_text": ["before"], "table_ori": [["Revenue", "2025"], ["Total", "10"]], "post_text": ["after"]}
    )
    assert "Revenue | 2025" in rendered
    assert "TEXT BEFORE TABLE" in rendered


def test_load_fire_preserves_canonical_tokens(tmp_path) -> None:
    data_path = tmp_path / "fire.json"
    types_path = tmp_path / "types.json"
    data_path.write_text(
        json.dumps(
            [
                {
                    "orig_id": 7,
                    "tokens": ["Acme", "sold", "chips", "."],
                    "entities": [
                        {"text": "Acme", "type": "Company", "start": 0, "end": 1},
                    ],
                    "relations": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    types_path.write_text(
        json.dumps({"entities": {"Company": {}}, "relations": {"ActionSell": {}}}),
        encoding="utf-8",
    )

    cases = load_fire(data_path, types_path, limit=1)

    assert cases[0].metadata["tokens"] == ["Acme", "sold", "chips", "."]
    assert cases[0].metadata["gold_entity_spans"] == [
        {"text": "Acme", "type": "Company", "start": 0, "end": 1}
    ]


def test_retrieve_fire_examples_prefers_rare_matching_terms() -> None:
    examples = [
        {"tokens": ["Acme", "sold", "chips"], "entities": [], "relations": []},
        {"tokens": ["Quarterly", "revenue", "increased"], "entities": [], "relations": []},
    ]

    selected = retrieve_fire_examples(
        {"tokens": ["The", "company", "sold", "its", "chips"]},
        examples,
        limit=1,
    )

    assert selected[0]["text"] == "Acme sold chips"

    bm25_selected = retrieve_fire_examples(
        {"tokens": ["The", "company", "sold", "its", "chips"]},
        examples,
        limit=1,
        strategy="bm25",
    )
    assert bm25_selected[0]["text"] == "Acme sold chips"
