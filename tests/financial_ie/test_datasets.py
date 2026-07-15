from valuechain.financial_ie.datasets import parse_ner_answer, rank_concept_labels, render_finqa_context


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
