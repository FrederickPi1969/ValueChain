from valuechain.financial_ie.multilingual.quality import audit_record, review_rows, summarize


def _record() -> dict:
    return {
        "status": "complete",
        "identity": {
            "source_id": "cninfo",
            "language": "zh-Hans",
            "issuer_name": "测试公司",
            "filing_id": "1",
            "filing_type": "annual_report",
            "source_url": "https://example.test",
            "document_granularity": "periodic_report",
        },
        "profile": {"business_summary_native": "", "evidence": []},
        "signals": [
            {
                "category": "capacity_and_supply",
                "modality": "current_fact",
                "chunk_id": "c1",
                "evidence_quote_native": "现有产能为十万吨",
                "evidence_valid": True,
                "evidence_failure_reason": "",
            }
        ],
        "relations": [],
        "evidence_chunks": [{"chunk_id": "c1", "text": "公司现有产能为十万吨。"}],
        "diagnostics": {
            "chunk_count": 1,
            "source_native_script_ratio": 1.0,
            "parser_warnings": [],
        },
    }


def test_summary_explicitly_records_no_database_writes() -> None:
    record = _record()
    summary = summarize([record], audit_record(record))
    assert summary["database_writes"] == 0
    assert summary["production_tables_touched"] == []
    assert summary["per_language"]["zh-Hans"]["signals"] == 1


def test_review_rows_include_source_chunk_and_blank_human_fields() -> None:
    rows = review_rows([_record()])
    assert rows[0]["source_chunk"] == "公司现有产能为十万吨。"
    assert rows[0]["human_label"] == ""
