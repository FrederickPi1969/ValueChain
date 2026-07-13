import hashlib
from dataclasses import replace

import pytest
import requests

from valuechain.sec_acquisition import (
    AcquisitionConfig,
    SecAcquisitionRunner,
    SecProxySession,
    download_atomic,
    parse_company_universe,
    parse_submission_columns,
    parse_target_years,
)


def test_company_universe_prioritizes_seed_and_deduplicates_cik() -> None:
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [1045810, "NVIDIA", "NVDA", "Nasdaq"],
            [1045810, "NVIDIA", "NVDL", "NYSE"],
            [1, "OTC Company", "OTCX", "OTC"],
        ],
    }

    rows = parse_company_universe(payload, {"NVDA": 0})

    assert len(rows) == 2
    assert rows[0].ticker == "NVDA"
    assert rows[0].priority == 0
    assert rows[1].priority == 500


def test_submission_parser_keeps_2026_tier_a_forms() -> None:
    columns = {
        "accessionNumber": ["0001-26-000001", "0001-25-000002", "0001-26-000003"],
        "filingDate": ["2026-01-04", "2025-12-31", "2026-02-01"],
        "form": ["10-K", "10-K", "DEF 14A"],
        "reportDate": ["2025-12-31", "2025-09-30", "2025-12-31"],
        "acceptanceDateTime": ["20260104120000", "", ""],
        "primaryDocument": ["annual.htm", "old.htm", "proxy.htm"],
    }

    rows = parse_submission_columns(columns, cik="0000000001", start_date="2026-01-01")

    assert [row["accession_number"] for row in rows] == ["0001-26-000001"]
    assert rows[0]["archive_url"].endswith("/1/000126000001/")


def test_submission_parser_does_not_mix_backfill_years() -> None:
    columns = {
        "accessionNumber": ["0001-26-000001", "0001-25-000002"],
        "filingDate": ["2026-01-04", "2025-12-31"],
        "form": ["10-K", "10-K"],
        "primaryDocument": ["new.htm", "old.htm"],
    }

    rows = parse_submission_columns(
        columns,
        cik="0000000001",
        start_date="2025-01-01",
        end_date="2025-12-31",
    )

    assert [row["accession_number"] for row in rows] == ["0001-25-000002"]


def test_target_years_preserve_order_and_reject_duplicates() -> None:
    assert parse_target_years("2026,2025") == (2026, 2025)

    with pytest.raises(ValueError):
        parse_target_years("2026,2026")


def test_environment_caps_request_concurrency_at_four(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_ACQUISITION_CONCURRENCY", "20")

    assert AcquisitionConfig.from_env().request_concurrency == 4


def test_environment_caps_request_retries_at_five(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_ACQUISITION_RETRIES", "20")

    assert AcquisitionConfig.from_env().request_retries == 5


def test_sec_session_makes_initial_attempt_plus_five_retries(monkeypatch) -> None:
    class Endpoint:
        url = "http://proxy.example:8080"

    class Pool:
        def random_normal(self) -> Endpoint:
            return Endpoint()

    class FailingSession:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise requests.ConnectionError("temporary failure")

    config = replace(
        AcquisitionConfig.from_env(),
        request_retries=5,
        request_sleep_seconds=0,
        request_jitter_seconds=0,
    )
    session = SecProxySession(config, Pool())
    failing = FailingSession()
    session.session = failing
    monkeypatch.setattr("valuechain.sec_acquisition.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="after proxy retries"):
        session.get("https://example.test", accept="application/json")

    assert failing.calls == 6


class FakeResponse:
    headers = {"content-type": "text/plain"}

    def iter_content(self, chunk_size: int):
        assert chunk_size > 0
        yield b"first"
        yield b"second"

    def close(self) -> None:
        pass


class FakeSession:
    def get(self, url: str, accept: str, stream: bool = False) -> FakeResponse:
        assert url == "https://example.test/document"
        assert stream is True
        return FakeResponse()


def test_download_atomic_writes_hash_and_removes_partial(tmp_path) -> None:
    target = tmp_path / "document.txt"

    result = download_atomic(FakeSession(), "https://example.test/document", target)

    assert target.read_bytes() == b"firstsecond"
    assert result["sha256"] == hashlib.sha256(b"firstsecond").hexdigest()
    assert result["byte_size"] == 11
    assert not (tmp_path / "document.txt.partial").exists()


def test_complete_submission_url_preserves_accession_dashes(tmp_path, monkeypatch) -> None:
    filing = {
        "cik": "0001045810",
        "accession_number": "0001045810-26-000003",
        "accession_no_dashes": "000104581026000003",
        "form": "8-K",
        "filing_date": "2026-01-23",
        "archive_url": "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000003/",
        "primary_document": "nvda-20260120.htm",
    }
    urls = []

    def fake_download(_session, url, path, accept="*/*"):
        urls.append(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return {
            "source_url": url,
            "local_path": str(path),
            "content_type": "text/plain",
            "byte_size": 1,
            "sha256": hashlib.sha256(b"x").hexdigest(),
            "retrieved_at": "2026-01-23T00:00:00+00:00",
            "status": "complete",
        }

    class FakeState:
        def upsert_filing(self, *args, **kwargs):
            pass

        def upsert_document(self, *args, **kwargs):
            pass

    monkeypatch.setattr("valuechain.sec_acquisition.download_atomic", fake_download)
    config = AcquisitionConfig(
        raw_root=tmp_path,
        state_path=tmp_path / "state.sqlite3",
        database_url="postgresql://test:test@127.0.0.1:5432/test",
        proxy_pool_url="https://proxy.example",
        sec_user_agent="test@example.com",
    )
    runner = SecAcquisitionRunner(config, tmp_path)

    runner.acquire_filing(FakeState(), object(), filing)

    assert any(url.endswith("/0001045810-26-000003.txt") for url in urls)
