from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_earnings_history_campaign as history_runner
from init_earnings_history_campaign import SCHEMA as INITIALIZER_SCHEMA
from run_earnings_history_campaign import (
    CampaignDownloaderTrialRunner,
    CampaignSchemaError,
    LostLeaseError,
    TrialOutcome,
    build_report,
    claim_job,
    connect_campaign,
    ensure_trial,
    finish_exhausted,
    heartbeat,
    query_policy,
    recover_stale_leases,
    rejected_artifact_matches_current_gate,
    run_workers,
    set_campaign_paused,
)

from valuechain.earnings_calls import Candidate, Judgement


def make_campaign(tmp_path: Path, *, jobs: int = 1, paused: bool = False) -> Path:
    path = tmp_path / "history.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(INITIALIZER_SCHEMA)
    stamp = "2026-08-11T00:00:00+00:00"
    db.execute(
        """
        INSERT INTO campaign (
          id,schema_version,mode,seed,pilot_size,annual_db,top_db,next_db,
          source_digest,paused,created_at,updated_at
        ) VALUES (1,1,'pilot','test-seed',100,'annual','top','next',
          'digest',?,?,?)
        """,
        (int(paused), stamp, stamp),
    )
    for index in range(1, jobs + 1):
        cik = f"{index:010d}"
        db.execute(
            """
            INSERT INTO companies (
              cik,ticker,company_name,sec_company_name,sector,sector_group,
              cohort,tier,source_company_id,source_db,incorporation_code,
              incorporation_normalized,company_priority,pilot_selected,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
            """,
            (
                cik,
                f"T{index}",
                f"Test Company {index}",
                f"SEC Test Company {index}",
                "Technology — Software",
                "Technology",
                "top",
                1,
                index,
                "top.sqlite3",
                "Delaware",
                "DE",
                index,
                stamp,
                stamp,
            ),
        )
        db.execute(
            """
            INSERT INTO jobs (
              cik,calendar_year,quarter,calendar_target,period_rank,priority,
              created_at,updated_at
            ) VALUES (?,2026,'Q2','2026-Q2',0,?,?,?)
            """,
            (cik, index, stamp, stamp),
        )
    db.commit()
    db.close()
    return path


@dataclass
class FakeResponse:
    candidates: list[Candidate]
    request_id: str | None = None


class FakeBackend:
    name = "duckduckgo"

    def __init__(
        self,
        responses: list[list[str] | BaseException],
        *,
        pause_db: Path | None = None,
    ) -> None:
        self.responses = responses
        self.pause_db = pause_db
        self.calls: list[str] = []

    async def search(self, query: str, *, limit: int = 10) -> FakeResponse:
        self.calls.append(query)
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, BaseException):
            raise response
        if self.pause_db is not None and len(self.calls) == 1:
            control = connect_campaign(self.pause_db)
            try:
                set_campaign_paused(control, True)
            finally:
                control.close()
        return FakeResponse(
            [
                Candidate(
                    url,
                    f"title {url}",
                    "earnings call transcript",
                    self.name,
                    query,
                )
                for url in response[:limit]
            ],
            request_id=f"ddg-{len(self.calls)}",
        )


async def judge_links(
    company: str, year: int, quarter: str, candidates: list[Candidate]
) -> list[Judgement]:
    del company, year, quarter
    return [
        Judgement(
            index,
            "negative" not in candidate.url,
            0.99 if "negative" not in candidate.url else 0.01,
            "third_party_transcript" if "negative" not in candidate.url else "other",
            "fake",
        )
        for index, candidate in enumerate(candidates)
    ]


class FakeTrial:
    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root
        self.calls: list[str] = []

    async def __call__(self, job, candidate, judgement) -> TrialOutcome:
        del judgement
        url = str(candidate["url"])
        self.calls.append(url)
        artifact = self.artifact_root / str(job["cik"]) / f"{candidate['id']}.zst"
        if url.endswith("/complete"):
            return TrialOutcome(
                "validated",
                fetch_method="fake",
                text_chars=50_000,
                artifact_path=str(artifact),
                validation={
                    "full_call": True,
                    "period_match": True,
                    "document_type": "earnings_call",
                    # A fiscal/result target can legitimately be called in the
                    # prior calendar year (for example MSFT FY26 Q1).
                    "call_date": "2025-10-29",
                },
            )
        return TrialOutcome(
            "rejected",
            fetch_method="fake",
            text_chars=20_000,
            validation={
                "full_call": False,
                "period_match": True,
                "document_type": "partial_call",
            },
        )


def test_query_policy_is_fixed_all_ddg_and_exactly_four() -> None:
    policy = query_policy("SMFG", "Sumitomo Mitsui Financial Group", 2026, "q1")
    assert [step.engine for step, _ in policy] == ["duckduckgo"] * 4
    assert [step.ordinal for step, _ in policy] == [1, 2, 3, 4]
    assert [query for _, query in policy] == [
        "SMFG Sumitomo Mitsui Financial Group 2026 Q1 earnings conference call",
        "SMFG Sumitomo Mitsui Financial Group 2026 Q1 earnings conference call YouTube",
        "SMFG Sumitomo Mitsui Financial Group 2026 Q1 earnings call transcript",
        "SMFG Sumitomo Mitsui Financial Group 2026 Q1 quarterly results conference call",
    ]


def test_non_campaign_schema_hard_fails_without_runtime_mutation(tmp_path: Path) -> None:
    database = tmp_path / "ab-study.sqlite3"
    db = sqlite3.connect(database)
    db.execute("CREATE TABLE arms(id INTEGER PRIMARY KEY,status TEXT)")
    db.commit()
    db.close()
    with pytest.raises(CampaignSchemaError, match="missing tables"):
        connect_campaign(database)
    db = sqlite3.connect(database)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    db.close()
    assert "history_runtime" not in tables
    assert "history_candidates" not in tables


def test_fenced_lease_heartbeat_pause_and_stale_recovery(tmp_path: Path) -> None:
    database = make_campaign(tmp_path, paused=True)
    first = connect_campaign(database)
    second = connect_campaign(database, add_runtime_schema=False)
    try:
        assert claim_job(first, "worker-a", lease_seconds=60) is None
        set_campaign_paused(first, False)
        old = claim_job(first, "worker-a", lease_seconds=60)
        assert old is not None
        assert claim_job(second, "worker-b", lease_seconds=60) is None
        heartbeat(
            first,
            int(old["id"]),
            str(old["lease_owner"]),
            str(old["lease_token"]),
            lease_seconds=120,
        )
        with pytest.raises(LostLeaseError):
            heartbeat(
                first,
                int(old["id"]),
                "worker-a",
                "wrong-token",
                lease_seconds=60,
            )

        first.execute(
            "UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (old["id"],),
        )
        assert recover_stale_leases(second) == 1
        new = claim_job(second, "worker-b", lease_seconds=60)
        assert new is not None
        assert new["lease_token"] != old["lease_token"]
        with pytest.raises(LostLeaseError):
            heartbeat(
                first,
                int(old["id"]),
                str(old["lease_owner"]),
                str(old["lease_token"]),
                lease_seconds=60,
            )
        with pytest.raises(LostLeaseError):
            finish_exhausted(first, old, "old worker must not commit")
        finish_exhausted(second, new, "bounded search exhausted")
        row = first.execute("SELECT * FROM jobs").fetchone()
        assert row["status"] == "exhausted"
        assert row["lease_owner"] is None and row["lease_token"] is None
        attempts = first.execute(
            "SELECT status FROM job_attempts ORDER BY attempt_no"
        ).fetchall()
        assert [row[0] for row in attempts] == ["expired", "succeeded"]
    finally:
        first.close()
        second.close()


def test_eight_workers_save_every_link_and_stop_on_first_strict_call(
    tmp_path: Path,
) -> None:
    database = make_campaign(tmp_path)
    backend = FakeBackend(
        [
            ["https://example.com/partial", "https://example.com/negative"],
            ["https://example.com/complete", "https://example.com/after-complete"],
        ]
    )
    trial = FakeTrial(tmp_path / "isolated-history-artifacts")
    result = asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=judge_links,
            trial_fn=trial,
            workers=8,
            limit=1,
        )
    )
    assert result["claimed"] == 1
    assert result["completed"] == 1
    assert len(backend.calls) == 2
    assert trial.calls == [
        "https://example.com/partial",
        "https://example.com/complete",
    ]

    db = connect_campaign(database)
    try:
        job = db.execute("SELECT * FROM jobs").fetchone()
        assert job["status"] == "completed"
        assert job["stage"] == "done"
        assert job["query_count"] == 2
        assert job["fiscal_year"] == 2026
        assert job["fiscal_quarter"] == "Q2"
        assert job["reported_period_label"] == "2026 Q2"
        assert job["call_date"] == "2025-10-29"
        assert job["lease_owner"] is None and job["lease_token"] is None
        assert db.execute("SELECT COUNT(*) FROM history_candidates").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM history_judgements").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM history_trials").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM history_results").fetchone()[0] == 1
        assert build_report(db)["max_job_query_count"] == 2
    finally:
        db.close()

    # Completed jobs are never claimed or searched again.
    asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=judge_links,
            trial_fn=trial,
            workers=8,
        )
    )
    assert len(backend.calls) == 2


def test_downloader_validates_reported_target_not_url_calendar_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(*args, **kwargs):
        del args, kwargs
        return ("Operator Q&A. This concludes the call.\n" * 200, "fake", {}, None)

    seen: list[tuple[int, str]] = []

    async def fake_validate(company, year, quarter, text):
        del company, text
        seen.append((year, quarter))
        return {
            "full_call": True,
            "period_match": True,
            "document_type": "earnings_call",
            "confidence": 0.99,
            "fiscal_year": 2025,
            "fiscal_quarter": "Q4",
            "call_date": "2025-10-29",
        }

    def fake_persist(staging, target, *args, **kwargs):
        del staging, args, kwargs
        target.mkdir(parents=True)
        artifact = target / "transcript.txt.zst"
        artifact.write_bytes(b"zstd-placeholder")
        return artifact

    monkeypatch.setattr(history_runner, "fetch_candidate", fake_fetch)
    monkeypatch.setattr(history_runner, "validate_target_transcript", fake_validate)
    monkeypatch.setattr(history_runner, "transcript_is_complete", lambda *a, **k: True)
    runner = CampaignDownloaderTrialRunner(
        campaign_db=tmp_path / "not-used.sqlite3",
        client=None,
        artifact_root=tmp_path / "artifacts",
        year=2026,
        quarter="Q1",
        extractor=None,
        fallback_lock=asyncio.Lock(),
    )
    monkeypatch.setattr(runner, "_persist_artifact", fake_persist)
    outcome = asyncio.run(
        runner(
            {
                "ticker": "MSFT",
                "company_name": "Microsoft Corporation",
            },
            {
                "id": 1,
                "engine": "duckduckgo",
                "query_ordinal": 1,
                "url": "https://example.com/2025/10/msft-fy26-q1-call",
                "title": "Microsoft FY26 Q1 earnings call — October 2025",
            },
            Judgement(0, True, 0.99, "third_party_transcript", "fake"),
        )
    )
    assert outcome.status == "validated"
    assert seen == [(2026, "Q1")]
    assert outcome.validation["call_date"] == "2025-10-29"
    assert outcome.validation["fiscal_year"] == 2026
    assert outcome.validation["fiscal_quarter"] == "Q1"


def test_four_normal_misses_are_exhausted_and_never_exceed_query_cap(
    tmp_path: Path,
) -> None:
    database = make_campaign(tmp_path)
    backend = FakeBackend([[], [], [], []])
    trial = FakeTrial(tmp_path / "artifacts")
    asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=judge_links,
            trial_fn=trial,
            workers=8,
        )
    )
    db = connect_campaign(database)
    try:
        job = db.execute("SELECT * FROM jobs").fetchone()
        assert job["status"] == "exhausted"
        assert job["query_count"] == 4
        assert db.execute("SELECT COUNT(*) FROM search_attempts").fetchone()[0] == 4
    finally:
        db.close()
    assert len(backend.calls) == 4
    assert trial.calls == []


def test_any_search_failure_makes_terminal_evidence_incomplete(
    tmp_path: Path,
) -> None:
    database = make_campaign(tmp_path)
    backend = FakeBackend([TimeoutError("DDG timeout"), [], [], []])
    trial = FakeTrial(tmp_path / "artifacts")
    result = asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=judge_links,
            trial_fn=trial,
            workers=8,
        )
    )
    assert result["failed"] == 1
    db = connect_campaign(database)
    try:
        job = db.execute("SELECT * FROM jobs").fetchone()
        assert job["status"] == "failed"
        assert job["error_class"] == "incomplete_search_evidence"
        assert job["query_count"] == 4
        assert db.execute(
            "SELECT COUNT(*) FROM search_attempts WHERE status='failed'"
        ).fetchone()[0] == 1
    finally:
        db.close()

    # Consumed ordinals are evidence and are never issued again implicitly.
    asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=judge_links,
            trial_fn=trial,
            workers=8,
        )
    )
    assert len(backend.calls) == 4


def test_qwen_retry_resumes_without_repeating_consumed_search(tmp_path: Path) -> None:
    database = make_campaign(tmp_path)
    backend = FakeBackend([["https://example.com/complete"]])
    trial = FakeTrial(tmp_path / "artifacts")
    judge_calls = 0

    async def flaky_judge(company, year, quarter, candidates):
        nonlocal judge_calls
        judge_calls += 1
        if judge_calls == 1:
            raise TimeoutError("local Qwen temporarily unavailable")
        return await judge_links(company, year, quarter, candidates)

    first = asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=flaky_judge,
            trial_fn=trial,
            workers=8,
        )
    )
    assert first["retry_wait"] == 1
    db = connect_campaign(database)
    db.execute("UPDATE jobs SET next_attempt_at='2000-01-01T00:00:00+00:00'")
    db.close()
    second = asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=flaky_judge,
            trial_fn=trial,
            workers=8,
        )
    )
    assert second["completed"] == 1
    assert len(backend.calls) == 1
    assert judge_calls == 2
    db = connect_campaign(database)
    try:
        attempts = db.execute(
            "SELECT status FROM history_judgement_attempts ORDER BY attempt_no"
        ).fetchall()
        assert [row[0] for row in attempts] == ["failed", "completed"]
        assert db.execute("SELECT query_count FROM jobs").fetchone()[0] == 1
    finally:
        db.close()


def test_candidate_retries_do_not_consume_job_infrastructure_budget(
    tmp_path: Path,
) -> None:
    database = make_campaign(tmp_path)
    db = connect_campaign(database)
    db.execute("UPDATE jobs SET max_attempts=3")
    db.close()
    backend = FakeBackend(
        [[f"https://example.com/candidate-{index}" for index in range(1, 5)]]
    )
    calls: list[str] = []

    async def transient_then_fourth(job, candidate, judgement):
        del job, judgement
        url = str(candidate["url"])
        calls.append(url)
        if not url.endswith("candidate-4"):
            raise TimeoutError(f"temporary candidate failure: {url}")
        return TrialOutcome(
            "validated",
            fetch_method="fake",
            text_chars=50_000,
            artifact_path=str(tmp_path / "candidate-4.txt.zst"),
            validation={
                "full_call": True,
                "period_match": True,
                "document_type": "earnings_call",
            },
        )

    for _ in range(3):
        result = asyncio.run(
            run_workers(
                database,
                backends={"duckduckgo": backend},
                judge_fn=judge_links,
                trial_fn=transient_then_fourth,
                workers=8,
                max_candidate_attempts=2,
            )
        )
        assert result["retry_wait"] == 1
        db = connect_campaign(database)
        try:
            job = db.execute("SELECT * FROM jobs").fetchone()
            assert job["status"] == "retry_wait"
            assert job["retry_count"] == 0
            db.execute(
                "UPDATE jobs SET next_attempt_at='2000-01-01T00:00:00+00:00'"
            )
            db.execute(
                """
                UPDATE history_trials
                SET next_attempt_at='2000-01-01T00:00:00+00:00'
                WHERE status='retry_wait'
                """
            )
        finally:
            db.close()

    final = asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=judge_links,
            trial_fn=transient_then_fourth,
            workers=8,
            max_candidate_attempts=2,
        )
    )
    assert final["completed"] == 1
    assert len(backend.calls) == 1
    assert calls == [
        "https://example.com/candidate-1",
        "https://example.com/candidate-1",
        "https://example.com/candidate-2",
        "https://example.com/candidate-2",
        "https://example.com/candidate-3",
        "https://example.com/candidate-3",
        "https://example.com/candidate-4",
    ]
    db = connect_campaign(database)
    try:
        job = db.execute("SELECT * FROM jobs").fetchone()
        assert job["status"] == "completed"
        assert job["attempt_count"] == 4 > job["max_attempts"]
        assert job["retry_count"] == 0
        assert job["query_count"] == 1
        first_three = db.execute(
            """
            SELECT c.url,t.attempt_no,t.status,t.error_category
            FROM history_trials t
            JOIN history_candidates c ON c.id=t.candidate_id
            WHERE c.url NOT LIKE '%candidate-4'
            ORDER BY c.url,t.attempt_no
            """
        ).fetchall()
        assert [(row["attempt_no"], row["status"]) for row in first_three] == [
            (1, "retry_wait"),
            (2, "download_failed"),
        ] * 3
        assert {row["error_category"] for row in first_three} == {
            "candidate_transient_infrastructure"
        }
    finally:
        db.close()


def test_pause_after_search_persists_results_and_resume_does_not_requery(
    tmp_path: Path,
) -> None:
    database = make_campaign(tmp_path)
    backend = FakeBackend(
        [["https://example.com/complete"]], pause_db=database
    )
    trial = FakeTrial(tmp_path / "artifacts")
    first = asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=judge_links,
            trial_fn=trial,
            workers=8,
        )
    )
    assert first["paused"] == 1
    db = connect_campaign(database)
    try:
        job = db.execute("SELECT * FROM jobs").fetchone()
        assert job["status"] == "pending"
        assert job["query_count"] == 1
        assert job["retry_count"] == 0
        assert db.execute("SELECT COUNT(*) FROM history_candidates").fetchone()[0] == 1
        set_campaign_paused(db, False)
    finally:
        db.close()
    second = asyncio.run(
        run_workers(
            database,
            backends={"duckduckgo": backend},
            judge_fn=judge_links,
            trial_fn=trial,
            workers=8,
        )
    )
    assert second["completed"] == 1
    assert len(backend.calls) == 1


def test_atomic_bundle_promotion_is_lease_fenced_and_never_partial(
    tmp_path: Path,
) -> None:
    database = make_campaign(tmp_path)
    db = connect_campaign(database)
    old = claim_job(db, "worker-old", lease_seconds=60)
    assert old is not None
    runner = CampaignDownloaderTrialRunner(
        campaign_db=database,
        client=None,
        artifact_root=tmp_path / "unused",
        year=2026,
        quarter="Q2",
        extractor=None,
        fallback_lock=asyncio.Lock(),
    )
    candidate = {
        "id": 7,
        "engine": "duckduckgo",
        "query_ordinal": 1,
        "url": "https://example.com/call",
    }
    judgement = Judgement(0, True, 0.99, "third_party_transcript", "fake")
    text = (
        "Operator: Welcome to the Q2 earnings call. This concludes the call.\n"
        * 500
    )
    staging = tmp_path / "ready-one"
    staging.mkdir()
    target = tmp_path / "history-artifacts" / "candidate-7"
    artifact = runner._persist_artifact(
        staging,
        target,
        old,
        candidate,
        judgement,
        text,
        "fake",
        {},
        None,
        {
            "full_call": True,
            "period_match": True,
            "document_type": "earnings_call",
            "confidence": 0.95,
            "period_end": "2025-09-30",
            "call_date": "2025-10-29",
        },
        True,
    )
    assert artifact.is_file()
    assert artifact.name == "transcript.txt.zst"
    assert artifact.parent.parent.name == "versions"
    assert not staging.exists()
    assert all(path.suffix == ".zst" for path in artifact.parent.iterdir())
    verified = history_runner.verify_v2_artifact(artifact.parent)
    assert verified is not None
    metadata = verified[1]
    assert metadata["year"] == metadata["target_year"] == 2026
    assert metadata["quarter"] == metadata["target_quarter"] == "Q2"
    assert metadata["fiscal_year"] == 2026
    assert metadata["fiscal_quarter"] == "Q2"
    assert metadata["validation_prompt_version"] == history_runner.VALIDATION_PROMPT_VERSION
    assert metadata["period_end"] == "2025-09-30"
    assert metadata["call_date"] == "2025-10-29"
    assert rejected_artifact_matches_current_gate(
        {
            "artifact_path": str(artifact),
            "validation_json": json.dumps(metadata["validation"]),
        },
        candidate,
        judgement,
    )

    db.execute(
        "UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (old["id"],),
    )
    assert recover_stale_leases(db) == 1
    new = claim_job(db, "worker-new", lease_seconds=60)
    assert new is not None
    staging_lost = tmp_path / "ready-lost"
    staging_lost.mkdir()
    lost_target = tmp_path / "history-artifacts" / "candidate-8"
    with pytest.raises(LostLeaseError):
        runner._persist_artifact(
            staging_lost,
            lost_target,
            old,
            {**candidate, "id": 8},
            judgement,
            text,
            "fake",
            {},
            None,
            {"full_call": True, "period_match": True, "document_type": "earnings_call"},
            True,
        )
    assert staging_lost.is_dir()
    assert not (lost_target / "versions").exists()
    db.close()


def test_gate_revalidation_skips_reverse_duplicate_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = make_campaign(tmp_path)
    db = connect_campaign(database)
    try:
        job = claim_job(db, "worker-a", lease_seconds=60)
        assert job is not None
        stamp = "2026-08-11T00:00:00+00:00"
        search_id = db.execute(
            """
            INSERT INTO search_attempts (
              job_id,ordinal,engine,query,status,result_count,started_at,finished_at
            ) VALUES (?,1,'duckduckgo','q','completed',2,?,?)
            """,
            (job["id"], stamp, stamp),
        ).lastrowid
        candidate_ids = []
        for ordinal in (0, 1):
            candidate_ids.append(
                db.execute(
                    """
                    INSERT INTO history_candidates (
                      job_id,search_attempt_id,result_ordinal,query_ordinal,engine,
                      url,normalized_url,title,snippet,source_type,discovered_at
                    ) VALUES (?,?,?,1,'duckduckgo',?,?,?,?,?,?)
                    """,
                    (
                        job["id"],
                        search_id,
                        ordinal,
                        f"https://example.com/call?copy={ordinal}",
                        "https://example.com/call",
                        "Test Company Q2 2026 earnings call",
                        "",
                        "webpage",
                        stamp,
                    ),
                ).lastrowid
            )
        judgement_attempt = db.execute(
            """
            INSERT INTO history_judgement_attempts (
              job_id,search_attempt_id,attempt_no,status,started_at,finished_at
            ) VALUES (?,?,1,'completed',?,?)
            """,
            (job["id"], search_id, stamp, stamp),
        ).lastrowid
        for candidate_id in candidate_ids:
            db.execute(
                """
                INSERT INTO history_judgements (
                  candidate_id,judgement_attempt_id,is_target,confidence,
                  content_kind,reason,judged_at
                ) VALUES (?,?,1,0.99,'third_party_transcript','test',?)
                """,
                (candidate_id, judgement_attempt, stamp),
            )
        db.execute(
            """
            INSERT INTO history_trials (
              candidate_id,job_id,candidate_rank,attempt_no,status,max_attempts,
              artifact_path,validation_json,finished_at,updated_at
            ) VALUES (?,?,1,1,'rejected',2,'old.zst','{}',?,?)
            """,
            (candidate_ids[0], job["id"], stamp, stamp),
        )
        db.execute(
            """
            INSERT INTO history_trials (
              candidate_id,job_id,candidate_rank,attempt_no,status,max_attempts,
              error_category,error,finished_at,updated_at
            ) VALUES (?,?,2,0,'duplicate',2,'duplicate_url','points to first',?,?)
            """,
            (candidate_ids[1], job["id"], stamp, stamp),
        )
        monkeypatch.setattr(
            history_runner,
            "rejected_artifact_matches_current_gate",
            lambda *args: True,
        )

        async def validate_again(*args) -> TrialOutcome:
            del args
            return TrialOutcome(
                "validated",
                artifact_path="new/transcript.txt.zst",
                validation={"full_call": True, "period_match": True},
            )

        candidate = db.execute(
            """
            SELECT c.*,j.is_target,j.confidence,j.content_kind,j.reason
            FROM history_candidates c
            JOIN history_judgements j ON j.candidate_id=c.id
            WHERE c.id=?
            """,
            (candidate_ids[0],),
        ).fetchone()
        outcome = asyncio.run(
            ensure_trial(
                db,
                job,
                candidate,
                Judgement(0, True, 0.99, "third_party_transcript", "test"),
                validate_again,
                candidate_rank=1,
                max_candidate_attempts=2,
            )
        )
        assert outcome is not None and outcome.status == "validated"
        assert db.execute(
            "SELECT status FROM history_trials WHERE candidate_id=? AND attempt_no=2",
            (candidate_ids[0],),
        ).fetchone()[0] == "validated"
        assert db.execute(
            "SELECT COUNT(*) FROM history_trials WHERE candidate_id=? AND attempt_no=0",
            (candidate_ids[0],),
        ).fetchone()[0] == 0
    finally:
        db.close()
