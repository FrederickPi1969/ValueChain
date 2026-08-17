import asyncio
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_earnings_call_pathfinder as pathfinder

from valuechain.earnings_call_publisher import (
    EarningsCallArtifactKey,
    PublishError,
    PublishResult,
    validate_artifact_bundle,
)


def complete_call_text() -> str:
    prepared = (
        "Chief Executive Officer: Revenue grew as customers adopted our products, "
        "and we continue to invest in durable long-term growth. "
    )
    return (
        "Operator: Welcome to Mock Corporation's Q1 2026 earnings conference call.\n"
        "Investor Relations: Today's discussion includes forward-looking statements.\n"
        + prepared
        * 80
        + "\nQuestion-and-Answer Session\n"
        "Analyst: Could you discuss demand?\n"
        "Chief Executive Officer: Demand remains healthy across our major markets.\n"
        "Operator: This concludes today's conference call. You may now disconnect."
    )


def test_validation_prompt_requires_evidence_grounded_period_metadata() -> None:
    system, user = pathfinder.validation_prompt(
        "Mock Corporation", 2026, "Q2", complete_call_text()
    )
    assert "Never infer a date" in system
    assert "Do not confuse the financial period end with the call date" in system
    for field in (
        "fiscal_year",
        "fiscal_quarter",
        "reported_period_label",
        "period_end",
        "call_date",
    ):
        assert f'"{field}"' in user


def test_validation_dates_are_typed_and_grounded_in_transcript() -> None:
    text = (
        "Our quarter ended June 30, 2026. "
        "This earnings call is being held on July 29th, 2026."
    )
    normalized = pathfinder.normalize_validation_metadata(
        {
            "fiscal_year": "2026",
            "fiscal_quarter": "q2",
            "reported_period_label": " FY 2026 second quarter ",
            "period_end": "2026-06-30",
            "call_date": "2026-07-30",
        },
        text,
    )
    assert normalized == {
        "fiscal_year": 2026,
        "fiscal_quarter": "Q2",
        "reported_period_label": "FY 2026 second quarter",
        "period_end": "2026-06-30",
        "call_date": None,
    }


def test_validator_retries_truncated_json_without_redownloading(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, config) -> None:
            del config
            self.calls = 0
            self.closed = False

        async def chat_json_async(self, system, user, max_tokens):
            del system
            self.calls += 1
            assert max_tokens == 1200
            if self.calls == 1:
                raise json.JSONDecodeError("truncated", '{"full_call":', 13)
            assert "previous response was invalid or truncated" in user
            return {
                "full_call": True,
                "confidence": 0.99,
                "document_type": "earnings_call",
                "period_match": True,
                "fiscal_year": 2025,
                "fiscal_quarter": "Q4",
                "reported_period_label": "2026 Q2",
                "period_end": "2026-06-30",
                "call_date": "2026-07-29",
                "opening_signals": ["operator"],
                "missing_or_problem": "none",
                "reason": "complete call",
            }

        async def aclose(self) -> None:
            self.closed = True

    clients: list[FakeClient] = []

    def make_client(config):
        client = FakeClient(config)
        clients.append(client)
        return client

    monkeypatch.setattr(pathfinder, "OpenAICompatibleClient", make_client)
    text = (
        complete_call_text()
        + "\nQuarter ended June 30, 2026. Call held July 29, 2026."
    )
    verdict = asyncio.run(
        pathfinder.validate("Mock Corporation", 2026, "Q2", text)
    )
    assert clients[0].calls == 2
    assert clients[0].closed
    assert verdict["period_end"] == "2026-06-30"
    assert verdict["call_date"] == "2026-07-29"
    assert verdict["fiscal_year"] == 2026
    assert verdict["fiscal_quarter"] == "Q2"


def make_database(path: Path) -> sqlite3.Connection:
    seed = sqlite3.connect(path)
    seed.execute(
        "CREATE TABLE companies ("
        "id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, company_name TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'completed')"
    )
    seed.execute(
        "CREATE TABLE accepted_urls ("
        "id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, url TEXT NOT NULL, "
        "title TEXT NOT NULL, content_kind TEXT NOT NULL, confidence REAL NOT NULL)"
    )
    seed.execute(
        "INSERT INTO companies(id,ticker,company_name) VALUES (1,'MOCK','Mock Corporation')"
    )
    seed.execute(
        "INSERT INTO accepted_urls(id,company_id,url,title,content_kind,confidence) "
        "VALUES (42,1,'https://example.com/mock-2026-q1-call',"
        "'Mock Q1 2026 earnings call','official_transcript',0.99)"
    )
    seed.commit()
    seed.close()
    return pathfinder.db_connect(path)


class FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Path, EarningsCallArtifactKey]] = []

    def publish(self, directory: Path, key: EarningsCallArtifactKey) -> PublishResult:
        validate_artifact_bundle(directory, key)
        self.calls.append((directory, key))
        if self.fail:
            raise PublishError("simulated publication failure")
        publication_uuid = uuid.UUID(int=1).hex
        current = (
            f"/cosmos/{key.year}/{key.quarter}/{key.ticker}/{key.candidate_id}/current"
        )
        return PublishResult(
            key=key,
            publication_uuid=publication_uuid,
            manifest_sha256="a" * 64,
            version_path=f"{current}/../versions/mock",
            current_path=current,
            current_target="versions/mock",
            file_count=3,
        )


def install_successful_fetch_and_validation(monkeypatch) -> None:
    async def fake_obtain(*args, **kwargs):
        return complete_call_text(), "mock.fetch"

    async def fake_validate(*args, **kwargs):
        return {
            "full_call": True,
            "confidence": 0.99,
            "document_type": "earnings_call",
            "period_match": True,
            "opening_signals": ["operator", "safe_harbor"],
            "missing_or_problem": "",
            "reason": "complete prepared remarks and Q&A",
        }

    monkeypatch.setattr(pathfinder, "obtain", fake_obtain)
    monkeypatch.setattr(pathfinder, "validate", fake_validate)


async def run_one(
    db: sqlite3.Connection,
    output: Path,
    publisher: FakePublisher | None,
) -> None:
    row = db.execute("SELECT * FROM companies WHERE id=1").fetchone()
    async with httpx.AsyncClient() as client:
        await pathfinder.process_company(
            db,
            row,
            2026,
            "Q1",
            output,
            "unused-profile",
            "unused-host",
            client,
            asyncio.Lock(),
            publisher,
        )


def test_validated_bundle_is_published_before_database_commit(
    tmp_path: Path, monkeypatch
) -> None:
    install_successful_fetch_and_validation(monkeypatch)
    db = make_database(tmp_path / "pathfinder.sqlite3")
    publisher = FakePublisher()
    try:
        asyncio.run(run_one(db, tmp_path / "artifacts", publisher))

        assert len(publisher.calls) == 1
        directory, key = publisher.calls[0]
        assert key == EarningsCallArtifactKey(2026, "Q1", "MOCK", 42)
        assert {path.name for path in directory.iterdir()} == {
            "manifest.json.zst",
            "metadata.json.zst",
            "transcript.txt.zst",
        }
        row = db.execute(
            "SELECT status,local_path,cosmos_path FROM pathfinder_transcripts"
        ).fetchone()
        assert row["status"] == "validated"
        assert row["local_path"].endswith("/transcript.txt.zst")
        assert row["cosmos_path"].endswith("/2026/Q1/MOCK/42/current")
        assert (
            db.execute(
                "SELECT status FROM pathfinder_company_status WHERE company_id=1"
            ).fetchone()["status"]
            == "validated"
        )
    finally:
        db.close()


def test_publish_failure_never_marks_candidate_or_company_validated(
    tmp_path: Path, monkeypatch
) -> None:
    install_successful_fetch_and_validation(monkeypatch)
    db = make_database(tmp_path / "pathfinder.sqlite3")
    publisher = FakePublisher(fail=True)
    try:
        asyncio.run(run_one(db, tmp_path / "artifacts", publisher))

        assert len(publisher.calls) == 1
        transcript = db.execute(
            "SELECT status,cosmos_path,validation_json FROM pathfinder_transcripts"
        ).fetchone()
        assert transcript["status"] == "download_failed"
        assert transcript["cosmos_path"] is None
        assert "simulated publication failure" in transcript["validation_json"]
        assert db.execute(
            "SELECT status FROM pathfinder_company_status WHERE company_id=1"
        ).fetchone() is None
        assert (
            db.execute(
                "SELECT count(*) FROM pathfinder_transcripts WHERE status='validated'"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()


def test_skip_cosmos_still_requires_a_valid_v2_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    install_successful_fetch_and_validation(monkeypatch)
    db = make_database(tmp_path / "pathfinder.sqlite3")
    try:
        asyncio.run(run_one(db, tmp_path / "artifacts", None))

        row = db.execute(
            "SELECT status,cosmos_path FROM pathfinder_transcripts"
        ).fetchone()
        assert row["status"] == "validated"
        assert row["cosmos_path"] is None
        validate_artifact_bundle(
            tmp_path / "artifacts" / "MOCK" / "42",
            EarningsCallArtifactKey(2026, "Q1", "MOCK", 42),
        )
    finally:
        db.close()
