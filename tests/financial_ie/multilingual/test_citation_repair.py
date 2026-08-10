from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.multilingual.citation_repair import (
    apply_repairs,
    collect_repair_items,
)


def test_collect_and_apply_exact_citation_repair() -> None:
    chunk = DocumentChunk("c1", "主要客户为甲公司，销售占比为22%。")
    signal = {
        "statement_native": "甲公司占销售额22%。",
        "chunk_id": "c1",
        "evidence_quote_native": "甲公司销售占比22%",
        "evidence_valid": False,
        "evidence_failure_reason": "quote_not_found",
        "modality": "current_fact",
        "review_status": "needs_review",
    }
    items = collect_repair_items({"evidence": []}, [signal], [], {"c1": chunk})
    assert items[0]["item_id"] == "signal:0"
    stats = apply_repairs(
        {
            "repairs": [
                {
                    "item_id": "signal:0",
                    "chunk_id": "c1",
                    "quote_native": "主要客户为甲公司，销售占比为22%",
                }
            ]
        },
        {"evidence": []},
        [signal],
        [],
        {"c1": chunk},
    )
    assert stats == {"requested": 1, "accepted": 1, "rejected": 0}
    assert signal["evidence_valid"] is True
    assert signal["evidence_repaired"] is True
    assert signal["evidence_quote_native_original"] == "甲公司销售占比22%"


def test_relation_repair_must_quote_the_object() -> None:
    chunk = DocumentChunk("c1", "公司向甲公司采购晶圆，并签订长期协议。")
    relation = {
        "subject_native": "公司",
        "object_native": "甲公司",
        "chunk_id": "c1",
        "evidence_quote_native": "错误引用",
        "evidence_valid": False,
        "modality": "current_fact",
    }
    stats = apply_repairs(
        {
            "repairs": [
                {
                    "item_id": "relation:0",
                    "chunk_id": "c1",
                    "quote_native": "并签订长期协议",
                }
            ]
        },
        {"evidence": []},
        [],
        [relation],
        {"c1": chunk},
    )
    assert stats["accepted"] == 0
    assert relation["evidence_valid"] is False
