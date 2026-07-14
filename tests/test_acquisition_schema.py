from pathlib import Path


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "acquisition_schema.sql"


def test_acquisition_schema_has_source_native_uniqueness_and_queue_indexes() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")

    assert "PRIMARY KEY (source_id, source_filing_id)" in sql
    assert "UNIQUE (source_id, source_url)" in sql
    assert "PRIMARY KEY (source_id, source_issuer_id, filing_year)" in sql
    assert "idx_acquisition_scan_queue" in sql
    assert "idx_acquisition_documents_hash" in sql
    assert "acquisition_universe_snapshots" in sql
    assert "UNIQUE (source_id, sha256)" in sql
    assert "acquisition_source_checkpoints" in sql
    assert "acquisition_api_usage" in sql
    assert "request_count <= request_limit" in sql
    assert "acquisition_source_objects" in sql
    assert "claimed_at TIMESTAMPTZ" in sql
    assert "next_attempt_at TIMESTAMPTZ" in sql
    assert "acquisition_ad_hoc_requests" in sql
    assert "request_key TEXT NOT NULL UNIQUE" in sql
    assert "idx_acquisition_ad_hoc_queue" in sql
