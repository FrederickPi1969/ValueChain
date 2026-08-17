import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import scripts.run_earnings_call_downstream as downloader
from scripts.run_earnings_call_downstream import (
    atomic_promote_verified_bundle,
    audit_existing,
    claim_job,
    conn,
    finish_failure,
    future,
    process_job,
    promote_and_finish_success,
    read_zstd,
    recover_stale_leases,
    select_and_seed_jobs,
    verify_v2_artifact,
    write_bundle_manifest,
)
from valuechain.earnings_call_artifacts import (
    compress_artifact_directory,
    compress_file,
)


def make_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE accepted_urls (id INTEGER PRIMARY KEY, url TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO accepted_urls(id,url) VALUES "
        "(1,'https://example.com/call'),(2,'https://youtu.be/example')"
    )
    db.commit()
    db.close()


def make_v2_bundle(path: Path, accepted_url_id: int) -> Path:
    path.mkdir(parents=True)
    text = ("Operator welcome. Analyst question. Management revenue guidance answer.\n" * 100)
    text_bytes = text.encode("utf-8")
    (path / "transcript.txt").write_bytes(text_bytes)
    (path / "metadata.json").write_text(
        json.dumps(
            {
                "artifact_schema_version": 2,
                "accepted_url_id": accepted_url_id,
                "text_chars": len(text),
                "text_sha256": hashlib.sha256(text_bytes).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    compress_artifact_directory(path)
    write_bundle_manifest(path, accepted_url_id)
    compress_artifact_directory(path)
    return path


def test_atomic_claim_allows_only_one_worker(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"
    make_database(database)
    setup = conn(database)
    assert select_and_seed_jobs(
        setup,
        accepted_url_ids=None,
        limit=10,
        max_attempts=3,
    ) == [1, 2]
    setup.close()

    first = conn(database)
    second = conn(database)
    try:
        job = claim_job(first, 1, "worker-a", max_attempts=3, lease_seconds=60)
        assert job is not None
        assert claim_job(second, 1, "worker-b", max_attempts=3, lease_seconds=60) is None
        finish_failure(first, job, RuntimeError("network timeout"), max_attempts=3)
        row = second.execute(
            "SELECT status,attempt_count,lease_owner,next_attempt_at FROM downstream_downloads "
            "WHERE accepted_url_id=1"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt_count"] == 1
        assert row["lease_owner"] is None
        assert row["next_attempt_at"] is not None
    finally:
        first.close()
        second.close()


def test_schema_migrates_legacy_downstream_table(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite"
    make_database(database)
    legacy = sqlite3.connect(database)
    legacy.execute(
        "CREATE TABLE downstream_downloads ("
        "accepted_url_id INTEGER PRIMARY KEY, source_url TEXT NOT NULL, source_kind TEXT NOT NULL, "
        "status TEXT NOT NULL, artifact_path TEXT, text_chars INTEGER, fetch_method TEXT, "
        "error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    legacy.commit()
    legacy.close()

    migrated = conn(database)
    try:
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(downstream_downloads)")
        }
        assert {"attempt_count", "lease_owner", "lease_expires_at", "artifact_sha256"} <= columns
    finally:
        migrated.close()


def test_expired_lease_is_recovered_and_reclaimable(tmp_path: Path) -> None:
    database = tmp_path / "stale.sqlite"
    make_database(database)
    db = conn(database)
    try:
        select_and_seed_jobs(db, accepted_url_ids="1", limit=1, max_attempts=3)
        first = claim_job(db, 1, "dead-worker", max_attempts=3, lease_seconds=60)
        assert first is not None
        db.execute(
            "UPDATE downstream_downloads SET lease_expires_at='2000-01-01T00:00:00+00:00' "
            "WHERE accepted_url_id=1"
        )
        assert recover_stale_leases(db) == 1
        second = claim_job(db, 1, "replacement", max_attempts=3, lease_seconds=60)
        assert second is not None
        assert second.attempt_no == 2
        attempt = db.execute(
            "SELECT status,error FROM downstream_attempts "
            "WHERE accepted_url_id=1 AND attempt_no=1"
        ).fetchone()
        assert attempt["status"] == "failed"
        assert "stale" in attempt["error"]
    finally:
        db.close()


def test_stale_recovery_does_not_clobber_a_renewed_lease(tmp_path: Path) -> None:
    database = tmp_path / "renewed.sqlite"
    make_database(database)
    db = conn(database)
    try:
        select_and_seed_jobs(db, accepted_url_ids="1", limit=1, max_attempts=3)
        assert claim_job(db, 1, "live-worker", max_attempts=3, lease_seconds=60)
        db.execute(
            "UPDATE downstream_downloads SET lease_expires_at='2000-01-01T00:00:00+00:00' "
            "WHERE accepted_url_id=1"
        )

        class RenewingCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(self):
                stale_snapshot = self.cursor.fetchall()
                db.execute(
                    "UPDATE downstream_downloads SET lease_expires_at=? "
                    "WHERE accepted_url_id=1",
                    (future(600),),
                )
                return stale_snapshot

        class RenewBeforeConditionalUpdate:
            def execute(self, sql, parameters=()):
                cursor = db.execute(sql, parameters)
                if sql.startswith("SELECT accepted_url_id, attempt_count, lease_owner"):
                    return RenewingCursor(cursor)
                return cursor

        assert recover_stale_leases(RenewBeforeConditionalUpdate()) == 0
        row = db.execute(
            "SELECT status,lease_owner,lease_expires_at FROM downstream_downloads "
            "WHERE accepted_url_id=1"
        ).fetchone()
        assert row["status"] == "running"
        assert row["lease_owner"] == "live-worker"
        assert row["lease_expires_at"] > future(500)
    finally:
        db.close()


def test_artifact_audit_grants_only_one_bounded_repair_budget(tmp_path: Path) -> None:
    database = tmp_path / "repair.sqlite"
    output = tmp_path / "artifacts"
    output.mkdir()
    make_database(database)
    db = conn(database)
    try:
        select_and_seed_jobs(db, accepted_url_ids="1", limit=1, max_attempts=3)
        db.execute(
            "UPDATE downstream_downloads SET status='downloaded',attempt_count=3 "
            "WHERE accepted_url_id=1"
        )
        assert audit_existing(db, output, max_repair_attempts=1) == (0, 1)
        repaired = db.execute(
            "SELECT status,attempt_count,repair_count FROM downstream_downloads "
            "WHERE accepted_url_id=1"
        ).fetchone()
        assert dict(repaired) == {
            "status": "failed",
            "attempt_count": 0,
            "repair_count": 1,
        }
        eligible = select_and_seed_jobs(
            db, accepted_url_ids=None, limit=10, max_attempts=3
        )
        assert 1 in eligible

        db.execute(
            "UPDATE downstream_downloads SET status='downloaded',attempt_count=3 "
            "WHERE accepted_url_id=1"
        )
        assert audit_existing(db, output, max_repair_attempts=1) == (0, 1)
        exhausted = db.execute(
            "SELECT status,attempt_count,repair_count FROM downstream_downloads "
            "WHERE accepted_url_id=1"
        ).fetchone()
        assert dict(exhausted) == {
            "status": "failed",
            "attempt_count": 3,
            "repair_count": 1,
        }
        exhausted_selection = select_and_seed_jobs(
            db, accepted_url_ids=None, limit=10, max_attempts=3
        )
        assert 1 not in exhausted_selection
    finally:
        db.close()


def test_v2_verifier_requires_identity_exact_membership_and_regular_files(
    tmp_path: Path,
) -> None:
    bundle = make_v2_bundle(tmp_path / "bundle", accepted_url_id=1)
    assert verify_v2_artifact(bundle, expected_accepted_url_id=1)
    assert not verify_v2_artifact(bundle, expected_accepted_url_id=2)

    extra = bundle / "extra.txt"
    extra.write_text("stale artifact", encoding="utf-8")
    compress_file(extra)
    assert not verify_v2_artifact(bundle, expected_accepted_url_id=1)
    (bundle / "extra.txt.zst").unlink()

    (bundle / "extra.zst").symlink_to(bundle / "transcript.txt.zst")
    assert not verify_v2_artifact(bundle, expected_accepted_url_id=1)
    (bundle / "extra.zst").unlink()

    manifest_path = bundle / "manifest.json.zst"
    manifest = json.loads(read_zstd(manifest_path, max_bytes=200_000))
    del manifest["files"]["transcript.txt.zst"]
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    compress_file(bundle / "manifest.json")
    assert not verify_v2_artifact(bundle, expected_accepted_url_id=1)


def test_atomic_version_promotion_is_fenced_by_the_lease(tmp_path: Path) -> None:
    database = tmp_path / "lease-fence.sqlite"
    make_database(database)
    db = conn(database)
    try:
        select_and_seed_jobs(db, accepted_url_ids="1", limit=1, max_attempts=3)
        job = claim_job(db, 1, "expired-worker", max_attempts=3, lease_seconds=60)
        assert job is not None
        db.execute(
            "UPDATE downstream_downloads SET lease_expires_at='2000-01-01T00:00:00+00:00' "
            "WHERE accepted_url_id=1"
        )
        ready = tmp_path / ".staging" / "ready"
        ready.mkdir(parents=True)
        (ready / "transcript.txt.zst").write_bytes(b"complete-ready-bundle")
        version = tmp_path / "1" / "versions" / "attempt-1-test"
        with pytest.raises(RuntimeError, match="lost or expired"):
            promote_and_finish_success(
                db,
                job,
                ready_directory=ready,
                version_directory=version,
                text_chars=100,
                method="test",
                sha256="a" * 64,
            )
        assert ready.is_dir()
        assert not version.exists()
        row = db.execute(
            "SELECT status,artifact_path FROM downstream_downloads WHERE accepted_url_id=1"
        ).fetchone()
        assert row["status"] == "running"
        assert row["artifact_path"] is None
    finally:
        db.close()


def test_database_failure_after_atomic_rename_leaves_only_a_complete_orphan(
    tmp_path: Path,
) -> None:
    database = tmp_path / "promotion-failure.sqlite"
    make_database(database)
    db = conn(database)
    try:
        select_and_seed_jobs(db, accepted_url_ids="1", limit=1, max_attempts=3)
        job = claim_job(db, 1, "worker-a", max_attempts=3, lease_seconds=60)
        assert job is not None
        ready = tmp_path / ".staging" / "ready"
        ready.mkdir(parents=True)
        expected = {
            "manifest.json.zst": b"manifest",
            "metadata.json.zst": b"metadata",
            "transcript.txt.zst": b"transcript",
        }
        for name, payload in expected.items():
            (ready / name).write_bytes(payload)
        version = tmp_path / "1" / "versions" / "attempt-1-test"

        class FailDatabaseCommitAfterRename:
            def execute(self, sql, parameters=()):
                if sql.startswith("UPDATE downstream_downloads SET status='downloaded'"):
                    raise sqlite3.OperationalError("simulated database failure")
                return db.execute(sql, parameters)

        with pytest.raises(sqlite3.OperationalError, match="simulated"):
            promote_and_finish_success(
                FailDatabaseCommitAfterRename(),
                job,
                ready_directory=ready,
                version_directory=version,
                text_chars=100,
                method="test",
                sha256="a" * 64,
            )
        assert not ready.exists()
        assert version.is_dir()
        assert {path.name: path.read_bytes() for path in version.iterdir()} == expected
        row = db.execute(
            "SELECT status,artifact_path FROM downstream_downloads WHERE accepted_url_id=1"
        ).fetchone()
        assert row["status"] == "running"
        assert row["artifact_path"] is None
    finally:
        db.close()


def test_atomic_promote_never_merges_into_an_existing_version(tmp_path: Path) -> None:
    ready = tmp_path / ".staging" / "ready"
    ready.mkdir(parents=True)
    (ready / "transcript.txt.zst").write_bytes(b"new")
    version = tmp_path / "1" / "versions" / "fixed-version"
    version.mkdir(parents=True)
    (version / "sentinel").write_bytes(b"old")

    with pytest.raises(RuntimeError, match="already exists"):
        atomic_promote_verified_bundle(ready, version)
    assert (ready / "transcript.txt.zst").read_bytes() == b"new"
    assert {path.name: path.read_bytes() for path in version.iterdir()} == {
        "sentinel": b"old"
    }


def test_process_job_publishes_one_complete_version_and_audits_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "process.sqlite"
    output = tmp_path / "artifacts"
    make_database(database)
    db = conn(database)
    try:
        select_and_seed_jobs(db, accepted_url_ids="1", limit=1, max_attempts=3)
        job = claim_job(db, 1, "worker-a", max_attempts=3, lease_seconds=60)
        assert job is not None
        text = (
            "Operator welcome. Analyst question. Management revenue guidance answer.\n"
            * 100
        )

        async def fake_fetch(*args, **kwargs):
            return text, "test.fetch", {}, None

        monkeypatch.setattr(downloader, "fetch_candidate", fake_fetch)

        async def run() -> bool:
            return await process_job(
                None,  # type: ignore[arg-type]
                db,
                job,
                output,
                None,
                asyncio.Lock(),
                force_opencli=False,
                lease_seconds=60,
                max_attempts=3,
            )

        assert asyncio.run(run())
        row = db.execute(
            "SELECT status,artifact_path FROM downstream_downloads WHERE accepted_url_id=1"
        ).fetchone()
        artifact = Path(row["artifact_path"])
        assert row["status"] == "downloaded"
        assert artifact.is_file()
        assert artifact.parent.parent.name == "versions"
        assert verify_v2_artifact(
            artifact.parent,
            expected_accepted_url_id=1,
        )
        assert [path.name for path in (output / "1").iterdir()] == ["versions"]
        assert audit_existing(db, output, max_repair_attempts=1) == (1, 0)
    finally:
        db.close()
