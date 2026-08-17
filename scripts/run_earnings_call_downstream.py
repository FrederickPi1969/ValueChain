"""Resumable earnings-call downloader with remote-only browser fallback.

Accepted URLs are fetched by the dedicated YouTube/PDF/ULSCAR paths first.
OpenCLI is a final fallback for ordinary web pages and always runs on a named
remote host; this worker never starts or controls a local browser.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import requests

from valuechain.earnings_call_artifacts import compress_artifact_directory
from valuechain.earnings_call_content import (
    normalize_transcript_text,
    transcript_quality_metrics,
    transcript_quality_problems,
)
from valuechain.earnings_calls import USER_AGENT, _rotating_proxy
from valuechain.remote_opencli import RemoteOpenCLIConfig, RemoteOpenCLIExtractor

VIDEO_URL = "http://100.114.26.88:13131/transcript"
ULSCR_URL = "http://100.114.26.88:23355"
ARTIFACT_SCHEMA_VERSION = 2
REQUIRED_BUNDLE_MEMBERS = frozenset({"transcript.txt.zst", "metadata.json.zst"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS downstream_downloads (
  accepted_url_id INTEGER PRIMARY KEY REFERENCES accepted_urls(id), source_url TEXT NOT NULL,
  source_kind TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','downloaded','failed')),
  artifact_path TEXT, text_chars INTEGER, fetch_method TEXT, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_downstream_status ON downstream_downloads(status);
CREATE TABLE IF NOT EXISTS downstream_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  accepted_url_id INTEGER NOT NULL REFERENCES accepted_urls(id),
  attempt_no INTEGER NOT NULL,
  worker_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('started','succeeded','failed')),
  fetch_method TEXT,
  text_chars INTEGER,
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(accepted_url_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS ix_downstream_attempt_url ON downstream_attempts(accepted_url_id, attempt_no);
"""

MIGRATION_COLUMNS = {
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "lease_owner": "TEXT",
    "lease_expires_at": "TEXT",
    "next_attempt_at": "TEXT",
    "last_started_at": "TEXT",
    "last_finished_at": "TEXT",
    "artifact_sha256": "TEXT",
    "repair_count": "INTEGER NOT NULL DEFAULT 0",
}


@dataclass(frozen=True)
class Job:
    accepted_url_id: int
    url: str
    source_kind: str
    attempt_no: int
    worker_id: str


def now() -> str:
    return datetime.now(UTC).isoformat()


def future(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def kind(url: str) -> str:
    lowered = url.lower().split("?", 1)[0]
    if "youtube.com/" in lowered or "youtu.be/" in lowered:
        return "youtube"
    if lowered.endswith(".pdf"):
        return "pdf"
    return "web"


def conn(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(downstream_downloads)")}
    for name, definition in MIGRATION_COLUMNS.items():
        if name not in columns:
            db.execute(f"ALTER TABLE downstream_downloads ADD COLUMN {name} {definition}")
    db.execute(
        "CREATE INDEX IF NOT EXISTS ix_downstream_retry "
        "ON downstream_downloads(status, next_attempt_at, attempt_count)"
    )
    return db


def zstd_is_valid(path: Path) -> bool:
    executable = shutil.which("zstd")
    if not executable or not path.is_file():
        return False
    completed = subprocess.run(
        [executable, "-q", "-t", str(path)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return completed.returncode == 0


def read_zstd(path: Path, *, max_bytes: int = 2_000_000) -> bytes:
    executable = shutil.which("zstd")
    if not executable:
        raise RuntimeError("zstd is unavailable")
    completed = subprocess.run(
        [executable, "-q", "-d", "-c", str(path)],
        capture_output=True,
        timeout=60,
        check=True,
    )
    if len(completed.stdout) > max_bytes:
        raise RuntimeError(f"decompressed artifact exceeds {max_bytes} bytes")
    return completed.stdout


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle_manifest(directory: Path, accepted_url_id: int) -> None:
    payload = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "accepted_url_id": accepted_url_id,
        "created_at": now(),
        "files": {
            path.name: {"bytes": path.stat().st_size, "sha256": file_sha256(path)}
            for path in sorted(directory.iterdir())
            if path.is_file() and path.suffix == ".zst"
        },
    }
    (directory / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def verify_v2_artifact(
    directory: Path,
    *,
    expected_accepted_url_id: int | None = None,
) -> tuple[Path, dict] | None:
    if directory.is_symlink() or not directory.is_dir():
        return None
    transcript = directory / "transcript.txt.zst"
    metadata = directory / "metadata.json.zst"
    manifest = directory / "manifest.json.zst"
    if not zstd_is_valid(transcript) or not zstd_is_valid(metadata) or not zstd_is_valid(manifest):
        return None
    try:
        payload = json.loads(read_zstd(metadata, max_bytes=200_000))
        bundle = json.loads(read_zstd(manifest, max_bytes=200_000))
        text_bytes = read_zstd(transcript)
        text = text_bytes.decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, RuntimeError, subprocess.SubprocessError):
        return None
    if payload.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
        return None
    if bundle.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
        return None
    manifest_id = bundle.get("accepted_url_id")
    metadata_id = payload.get("accepted_url_id")
    if (
        isinstance(manifest_id, bool)
        or not isinstance(manifest_id, int)
        or isinstance(metadata_id, bool)
        or not isinstance(metadata_id, int)
        or metadata_id != manifest_id
        or (
            expected_accepted_url_id is not None
            and manifest_id != expected_accepted_url_id
        )
    ):
        return None
    files = bundle.get("files")
    if (
        not isinstance(files, dict)
        or not files
        or not REQUIRED_BUNDLE_MEMBERS.issubset(files)
        or "manifest.json.zst" in files
    ):
        return None
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return None
    actual_names: set[str] = set()
    for path in entries:
        try:
            mode = path.lstat().st_mode
        except OSError:
            return None
        if path.is_symlink() or not stat.S_ISREG(mode):
            return None
        actual_names.add(path.name)
    if actual_names != set(files) | {"manifest.json.zst"}:
        return None
    for filename, expected in files.items():
        if (
            not isinstance(filename, str)
            or not filename
            or filename != Path(filename).name
            or filename in {".", ".."}
            or "\\" in filename
            or not isinstance(expected, dict)
        ):
            return None
        path = directory / filename
        expected_bytes = expected.get("bytes")
        expected_sha256 = expected.get("sha256")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes <= 0
            or not isinstance(expected_sha256, str)
            or not SHA256_PATTERN.fullmatch(expected_sha256)
            or path.stat().st_size != expected_bytes
            or file_sha256(path) != expected_sha256
            or path.suffix != ".zst"
            or not zstd_is_valid(path)
        ):
            return None
    if payload.get("text_sha256") != hashlib.sha256(text_bytes).hexdigest():
        return None
    if payload.get("text_chars") != len(text) or transcript_quality_problems(text):
        return None
    return transcript, payload


def audit_existing(
    db: sqlite3.Connection,
    output: Path,
    *,
    max_repair_attempts: int = 1,
) -> tuple[int, int]:
    valid = invalid = 0
    rows = db.execute(
        "SELECT accepted_url_id,artifact_path,attempt_count,repair_count "
        "FROM downstream_downloads WHERE status='downloaded' ORDER BY accepted_url_id"
    ).fetchall()
    for row in rows:
        accepted_url_id = int(row["accepted_url_id"])
        artifact_path = row["artifact_path"]
        directory = Path(artifact_path).parent if artifact_path else output / str(accepted_url_id)
        verified = verify_v2_artifact(
            directory,
            expected_accepted_url_id=accepted_url_id,
        )
        if verified:
            artifact, metadata = verified
            db.execute(
                "UPDATE downstream_downloads SET artifact_path=?, text_chars=?, artifact_sha256=?, "
                "error=NULL, updated_at=? WHERE accepted_url_id=?",
                (
                    str(artifact),
                    metadata["text_chars"],
                    metadata["text_sha256"],
                    now(),
                    accepted_url_id,
                ),
            )
            valid += 1
        else:
            repair_count = int(row["repair_count"] or 0)
            grant_repair = repair_count < max_repair_attempts
            db.execute(
                "UPDATE downstream_downloads SET status='failed', artifact_path=NULL, "
                "artifact_sha256=NULL, text_chars=NULL, "
                "error='artifact audit failed or legacy artifact needs v2 refetch', "
                "attempt_count=?, repair_count=?, next_attempt_at=NULL, updated_at=? "
                "WHERE accepted_url_id=?",
                (
                    0 if grant_repair else int(row["attempt_count"] or 0),
                    repair_count + 1 if grant_repair else repair_count,
                    now(),
                    accepted_url_id,
                ),
            )
            invalid += 1
    return valid, invalid


def recover_stale_leases(db: sqlite3.Connection) -> int:
    recovered = 0
    db.execute("BEGIN IMMEDIATE")
    try:
        stamp = now()
        stale = db.execute(
            "SELECT accepted_url_id, attempt_count, lease_owner FROM downstream_downloads "
            "WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
            (stamp,),
        ).fetchall()
        for row in stale:
            updated = db.execute(
                "UPDATE downstream_downloads SET status='failed', lease_owner=NULL, "
                "lease_expires_at=NULL, error='stale worker lease recovered', "
                "next_attempt_at=NULL, updated_at=? WHERE accepted_url_id=? "
                "AND status='running' AND lease_owner IS ? "
                "AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (stamp, row["accepted_url_id"], row["lease_owner"], stamp),
            ).rowcount
            if not updated:
                continue
            recovered += 1
            db.execute(
                "UPDATE downstream_attempts SET status='failed', "
                "error='stale worker lease recovered', finished_at=? "
                "WHERE accepted_url_id=? AND attempt_no=? AND worker_id IS ? "
                "AND status='started'",
                (
                    stamp,
                    row["accepted_url_id"],
                    row["attempt_count"],
                    row["lease_owner"],
                ),
            )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return recovered


def select_and_seed_jobs(
    db: sqlite3.Connection,
    *,
    accepted_url_ids: str | None,
    limit: int,
    max_attempts: int,
) -> list[int]:
    if accepted_url_ids:
        ids = sorted({int(value) for value in accepted_url_ids.split(",") if value.strip()})
        placeholders = ",".join("?" for _ in ids)
        rows = db.execute(
            f"SELECT id, url FROM accepted_urls WHERE id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT a.id, a.url FROM accepted_urls a "
            "LEFT JOIN downstream_downloads d ON d.accepted_url_id=a.id "
            "WHERE d.accepted_url_id IS NULL OR "
            "(d.status IN ('pending','failed') AND COALESCE(d.attempt_count,0)<? "
            "AND (d.next_attempt_at IS NULL OR d.next_attempt_at<=?)) "
            "ORDER BY a.id LIMIT ?",
            (max_attempts, now(), limit),
        ).fetchall()
    stamp = now()
    for row in rows:
        source_kind = kind(row["url"])
        db.execute(
            "INSERT OR IGNORE INTO downstream_downloads "
            "(accepted_url_id,source_url,source_kind,status,created_at,updated_at) "
            "VALUES (?,?,?,'pending',?,?)",
            (row["id"], row["url"], source_kind, stamp, stamp),
        )
        db.execute(
            "UPDATE downstream_downloads SET source_url=?, source_kind=?, updated_at=? "
            "WHERE accepted_url_id=?",
            (row["url"], source_kind, stamp, row["id"]),
        )
    return [int(row["id"]) for row in rows]


def claim_job(
    db: sqlite3.Connection,
    accepted_url_id: int,
    worker_id: str,
    *,
    max_attempts: int,
    lease_seconds: int,
) -> Job | None:
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT * FROM downstream_downloads WHERE accepted_url_id=?",
            (accepted_url_id,),
        ).fetchone()
        stamp = now()
        if row is None or row["status"] == "downloaded":
            db.execute("COMMIT")
            return None
        if row["status"] == "running" and row["lease_expires_at"] and row["lease_expires_at"] > stamp:
            db.execute("COMMIT")
            return None
        if row["status"] == "running":
            db.execute(
                "UPDATE downstream_attempts SET status='failed', "
                "error='expired lease reclaimed during claim', finished_at=? "
                "WHERE accepted_url_id=? AND attempt_no=? AND worker_id IS ? "
                "AND status='started'",
                (
                    stamp,
                    accepted_url_id,
                    int(row["attempt_count"] or 0),
                    row["lease_owner"],
                ),
            )
        attempt_no = int(row["attempt_count"] or 0) + 1
        if attempt_no > max_attempts or (row["next_attempt_at"] and row["next_attempt_at"] > stamp):
            db.execute("COMMIT")
            return None
        updated = db.execute(
            "UPDATE downstream_downloads SET status='running', attempt_count=?, lease_owner=?, "
            "lease_expires_at=?, last_started_at=?, updated_at=? WHERE accepted_url_id=? "
            "AND status<>'downloaded'",
            (attempt_no, worker_id, future(lease_seconds), stamp, stamp, accepted_url_id),
        ).rowcount
        if not updated:
            db.execute("ROLLBACK")
            return None
        db.execute(
            "INSERT OR REPLACE INTO downstream_attempts "
            "(accepted_url_id,attempt_no,worker_id,status,started_at) VALUES (?,?,?,'started',?)",
            (accepted_url_id, attempt_no, worker_id, stamp),
        )
        db.execute("COMMIT")
        return Job(accepted_url_id, str(row["source_url"]), str(row["source_kind"]), attempt_no, worker_id)
    except Exception:
        db.execute("ROLLBACK")
        raise


def heartbeat(db: sqlite3.Connection, job: Job, lease_seconds: int) -> None:
    db.execute(
        "UPDATE downstream_downloads SET lease_expires_at=?, updated_at=? "
        "WHERE accepted_url_id=? AND status='running' AND lease_owner=?",
        (future(lease_seconds), now(), job.accepted_url_id, job.worker_id),
    )


def finish_success(
    db: sqlite3.Connection,
    job: Job,
    *,
    artifact: Path,
    text_chars: int,
    method: str,
    sha256: str,
) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        stamp = now()
        updated = db.execute(
            "UPDATE downstream_downloads SET status='downloaded', artifact_path=?, text_chars=?, "
            "fetch_method=?, artifact_sha256=?, error=NULL, lease_owner=NULL, lease_expires_at=NULL, "
            "next_attempt_at=NULL, last_finished_at=?, updated_at=? "
            "WHERE accepted_url_id=? AND status='running' AND lease_owner=? "
            "AND lease_expires_at>?",
            (
                str(artifact),
                text_chars,
                method,
                sha256,
                stamp,
                stamp,
                job.accepted_url_id,
                job.worker_id,
                stamp,
            ),
        ).rowcount
        if not updated:
            raise RuntimeError("worker lost its lease before committing the artifact")
        db.execute(
            "UPDATE downstream_attempts SET status='succeeded', fetch_method=?, text_chars=?, "
            "finished_at=? WHERE accepted_url_id=? AND attempt_no=?",
            (method, text_chars, stamp, job.accepted_url_id, job.attempt_no),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def atomic_promote_verified_bundle(
    ready_directory: Path,
    version_directory: Path,
) -> Path:
    """Expose a pre-verified ready directory with one same-filesystem rename."""
    if not ready_directory.is_dir() or ready_directory.is_symlink():
        raise RuntimeError("ready artifact directory is unavailable")
    if os.path.lexists(version_directory):
        raise RuntimeError("immutable artifact version already exists")
    version_directory.parent.mkdir(parents=True, exist_ok=True)
    if ready_directory.stat().st_dev != version_directory.parent.stat().st_dev:
        raise RuntimeError("ready and version directories must share a filesystem")
    ready_directory.rename(version_directory)
    return version_directory


def promote_and_finish_success(
    db: sqlite3.Connection,
    job: Job,
    *,
    ready_directory: Path,
    version_directory: Path,
    text_chars: int,
    method: str,
    sha256: str,
) -> Path:
    """Fence the lease, atomically expose one immutable version, then commit it.

    ``ready_directory`` and ``version_directory`` must be on the same filesystem.
    A database failure after the rename can leave a complete orphan version, but
    it can never expose a partial bundle or overwrite another worker's version.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        stamp = now()
        lease = db.execute(
            "SELECT status,lease_owner,lease_expires_at FROM downstream_downloads "
            "WHERE accepted_url_id=?",
            (job.accepted_url_id,),
        ).fetchone()
        if (
            lease is None
            or lease["status"] != "running"
            or lease["lease_owner"] != job.worker_id
            or not lease["lease_expires_at"]
            or lease["lease_expires_at"] <= stamp
        ):
            raise RuntimeError("worker lost or expired its lease before artifact promotion")

        atomic_promote_verified_bundle(ready_directory, version_directory)
        artifact = version_directory / "transcript.txt.zst"
        updated = db.execute(
            "UPDATE downstream_downloads SET status='downloaded', artifact_path=?, "
            "text_chars=?, fetch_method=?, artifact_sha256=?, error=NULL, "
            "lease_owner=NULL, lease_expires_at=NULL, next_attempt_at=NULL, "
            "last_finished_at=?, updated_at=? WHERE accepted_url_id=? "
            "AND status='running' AND lease_owner=? AND lease_expires_at>?",
            (
                str(artifact),
                text_chars,
                method,
                sha256,
                stamp,
                stamp,
                job.accepted_url_id,
                job.worker_id,
                stamp,
            ),
        ).rowcount
        if not updated:
            raise RuntimeError("worker lost its lease while promoting the artifact")
        attempt_updated = db.execute(
            "UPDATE downstream_attempts SET status='succeeded', fetch_method=?, "
            "text_chars=?, finished_at=? WHERE accepted_url_id=? AND attempt_no=? "
            "AND worker_id=? AND status='started'",
            (
                method,
                text_chars,
                stamp,
                job.accepted_url_id,
                job.attempt_no,
                job.worker_id,
            ),
        ).rowcount
        if attempt_updated != 1:
            raise RuntimeError("attempt history disappeared during artifact promotion")
        db.execute("COMMIT")
        return artifact
    except Exception:
        db.execute("ROLLBACK")
        raise


def finish_failure(db: sqlite3.Connection, job: Job, error: Exception, *, max_attempts: int) -> None:
    stamp = now()
    detail = f"{type(error).__name__}: {error}"[-4_000:]
    retry_delay = min(3_600, 30 * (2 ** max(0, job.attempt_no - 1)))
    next_attempt = future(retry_delay) if job.attempt_no < max_attempts else None
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "UPDATE downstream_downloads SET status='failed', error=?, lease_owner=NULL, "
            "lease_expires_at=NULL, next_attempt_at=?, last_finished_at=?, updated_at=? "
            "WHERE accepted_url_id=? AND lease_owner=?",
            (detail, next_attempt, stamp, stamp, job.accepted_url_id, job.worker_id),
        )
        db.execute(
            "UPDATE downstream_attempts SET status='failed', error=?, finished_at=? "
            "WHERE accepted_url_id=? AND attempt_no=?",
            (detail, stamp, job.accepted_url_id, job.attempt_no),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


async def video_transcript(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    response = await client.post(
        VIDEO_URL,
        json={"url": url, "languages": ["en", "en-US"]},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success") or not payload.get("transcript_text"):
        raise RuntimeError(payload.get("warning") or "video service returned no transcript")
    return str(payload["transcript_text"]), "video.transcript"


def download_pdf(url: str, directory: Path) -> tuple[str, str]:
    proxies = _rotating_proxy()
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        proxies=proxies,
        timeout=(20, 90),
        stream=True,
    )
    response.raise_for_status()
    partial = directory / "source.pdf.partial"
    size = 0
    with partial.open("wb") as handle:
        for chunk in response.iter_content(1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > 100 * 1024 * 1024:
                raise RuntimeError("PDF exceeds the 100 MiB safety limit")
            handle.write(chunk)
    with partial.open("rb") as handle:
        magic = handle.read(4)
    if magic != b"%PDF":
        partial.unlink(missing_ok=True)
        raise RuntimeError("PDF URL did not return a PDF")
    pdf = directory / "source.pdf"
    partial.replace(pdf)
    text_path = directory / "transcript.txt"
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext is unavailable")
    completed = subprocess.run(
        ["pdftotext", "-layout", str(pdf), str(text_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode or not text_path.exists() or text_path.stat().st_size < 500:
        raise RuntimeError(f"PDF text extraction failed: {completed.stderr[-500:]}")
    return text_path.read_text(encoding="utf-8", errors="ignore"), "pdf.pdftotext"


async def scrape_web(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    request = {
        "urls": [url],
        "min_chars": 500,
        "use_proxy": True,
        "return_markdown": False,
        "fallbacks": {"naive": True, "headless": True, "head": False, "jina": True},
    }
    response = await client.post(f"{ULSCR_URL}/scrape_sync", json=request, timeout=60)
    response.raise_for_status()
    submitted = response.json()
    job_id = submitted["results"][0]["job_id"]
    for _ in range(60):
        await asyncio.sleep(1)
        response = await client.post(
            f"{ULSCR_URL}/results_batch",
            json={"job_ids": [job_id], "include_text": True, "include_attempts": True},
            timeout=60,
        )
        response.raise_for_status()
        item = response.json()["results"][0]
        if item["status"] == "complete":
            result = item["result"]
            if result.get("success") and len(result.get("text", "")) >= 500:
                method = result.get("selected_attempt", result.get("method", "unknown"))
                return str(result["text"]), f"ulscr.{method}"
            raise RuntimeError(result.get("error_type") or "ULSCR returned insufficient text")
        if item["status"] == "failed":
            raise RuntimeError(str(item))
    raise RuntimeError("ULSCR polling timed out after 60 seconds")


def opencli_extract(
    url: str,
    profile: str,
    session: str,
    *,
    host: str,
    executable: str = "/opt/homebrew/bin/opencli",
    helper_executable: str = "/Users/frederickpi/.local/bin/valuechain-opencli-extract",
) -> tuple[str, str]:
    """Compatibility wrapper that deliberately has no local execution mode."""
    extractor = RemoteOpenCLIExtractor(
        RemoteOpenCLIConfig(
            host=host,
            profile=profile,
            executable=executable,
            helper_executable=helper_executable,
        )
    )
    return extractor.extract(url, session)


def clean_and_gate(raw: str) -> tuple[str, dict[str, int | float]]:
    text = normalize_transcript_text(raw)
    metrics = transcript_quality_metrics(text)
    problems = transcript_quality_problems(text)
    if problems:
        raise RuntimeError("post-processing quality gate failed: " + "; ".join(problems))
    return text, metrics


async def fetch_candidate(
    client: httpx.AsyncClient,
    job: Job,
    staging: Path,
    extractor: RemoteOpenCLIExtractor | None,
    fallback_lock: asyncio.Lock,
    *,
    force_opencli: bool,
    opencli_reason: str | None = None,
) -> tuple[str, str, dict[str, int | float], str | None]:
    if job.source_kind == "youtube":
        raw, method = await video_transcript(client, job.url)
        text, metrics = clean_and_gate(raw)
        return text, method, metrics, None
    if job.source_kind == "pdf":
        raw, method = await asyncio.to_thread(download_pdf, job.url, staging)
        text, metrics = clean_and_gate(raw)
        return text, method, metrics, None

    primary_error: Exception | None = None
    if not force_opencli:
        try:
            raw, method = await scrape_web(client, job.url)
            if raw.lstrip().startswith("%PDF"):
                raw, method = await asyncio.to_thread(download_pdf, job.url, staging)
            text, metrics = clean_and_gate(raw)
            return text, method, metrics, None
        except Exception as exc:  # noqa: BLE001 - any primary extractor failure triggers the bounded fallback
            primary_error = exc
    else:
        primary_error = RuntimeError(
            opencli_reason or "ULSCAR bypassed by explicit --force-opencli"
        )

    if extractor is None:
        raise RuntimeError(f"ULSCAR failed and remote OpenCLI is not configured: {primary_error}")
    async with fallback_lock:
        raw, method = await asyncio.to_thread(
            extractor.extract,
            job.url,
            f"vc-earnings-{job.accepted_url_id}-{job.attempt_no}",
        )
    text, metrics = clean_and_gate(raw)
    return text, method, metrics, f"{type(primary_error).__name__}: {primary_error}"


async def heartbeat_loop(
    db: sqlite3.Connection,
    job: Job,
    lease_seconds: int,
) -> None:
    interval = max(10, min(60, lease_seconds // 3))
    while True:
        await asyncio.sleep(interval)
        heartbeat(db, job, lease_seconds)


async def process_job(
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    job: Job,
    output: Path,
    extractor: RemoteOpenCLIExtractor | None,
    fallback_lock: asyncio.Lock,
    *,
    force_opencli: bool,
    lease_seconds: int,
    max_attempts: int,
) -> bool:
    heartbeater = asyncio.create_task(heartbeat_loop(db, job, lease_seconds))
    staging: Path | None = None
    try:
        staging_parent = output / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(
            dir=staging_parent,
            prefix=f"{job.accepted_url_id}-{job.attempt_no}-",
        ))
        text, method, quality, primary_error = await fetch_candidate(
            client,
            job,
            staging,
            extractor,
            fallback_lock,
            force_opencli=force_opencli,
        )
        text_bytes = text.encode("utf-8")
        digest = hashlib.sha256(text_bytes).hexdigest()
        transcript = staging / "transcript.txt"
        transcript.write_bytes(text_bytes)
        metadata = {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "accepted_url_id": job.accepted_url_id,
            "source_url": job.url,
            "source_kind": job.source_kind,
            "fetch_method": method,
            "fetched_at": now(),
            "text_chars": len(text),
            "text_sha256": digest,
            "quality": quality,
            "primary_error": primary_error,
        }
        (staging / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await asyncio.to_thread(compress_artifact_directory, staging)
        await asyncio.to_thread(write_bundle_manifest, staging, job.accepted_url_id)
        await asyncio.to_thread(compress_artifact_directory, staging)
        verified = await asyncio.to_thread(
            verify_v2_artifact,
            staging,
            expected_accepted_url_id=job.accepted_url_id,
        )
        if not verified:
            raise RuntimeError("ready artifact failed manifest/hash/quality verification")

        version_id = f"attempt-{job.attempt_no}-{uuid.uuid4().hex}"
        version = output / str(job.accepted_url_id) / "versions" / version_id
        promote_and_finish_success(
            db,
            job,
            ready_directory=staging,
            version_directory=version,
            text_chars=len(text),
            method=method,
            sha256=digest,
        )
        staging = None
        print(
            f"DOWNLOADED id={job.accepted_url_id} kind={job.source_kind} "
            f"method={method} chars={len(text)} attempt={job.attempt_no}",
            flush=True,
        )
        return True
    except asyncio.CancelledError:
        finish_failure(db, job, RuntimeError("worker cancelled"), max_attempts=max_attempts)
        raise
    except Exception as exc:  # noqa: BLE001 - worker boundary persists every failure in SQLite
        finish_failure(db, job, exc, max_attempts=max_attempts)
        print(
            f"FAILED id={job.accepted_url_id} kind={job.source_kind} "
            f"attempt={job.attempt_no}: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return False
    finally:
        heartbeater.cancel()
        await asyncio.gather(heartbeater, return_exceptions=True)
        if staging is not None and staging.exists():
            await asyncio.to_thread(shutil.rmtree, staging, True)


async def main_async(args: argparse.Namespace) -> None:
    setup = conn(args.db)
    try:
        recovered = recover_stale_leases(setup)
        if recovered:
            print(f"recovered_stale_leases={recovered}", flush=True)
        if args.audit_existing:
            valid, invalid = audit_existing(
                setup,
                args.output_dir,
                max_repair_attempts=args.max_repair_attempts,
            )
            print(f"artifact_audit valid_v2={valid} invalid_or_legacy={invalid}", flush=True)
        ids = select_and_seed_jobs(
            setup,
            accepted_url_ids=args.accepted_url_ids,
            limit=args.limit,
            max_attempts=args.max_attempts,
        )
    finally:
        setup.close()

    extractor = None
    if args.opencli_host and args.opencli_profile:
        extractor = RemoteOpenCLIExtractor(
            RemoteOpenCLIConfig(
                host=args.opencli_host,
                profile=args.opencli_profile,
                executable=args.opencli_executable,
                helper_executable=args.opencli_helper,
            )
        )
    print(
        f"selected={len(ids)} workers={args.workers} browser_fallback="
        f"{args.opencli_host or 'disabled'} (remote-only)",
        flush=True,
    )
    queue: asyncio.Queue[int] = asyncio.Queue()
    for accepted_url_id in ids:
        queue.put_nowait(accepted_url_id)
    fallback_lock = asyncio.Lock()
    totals = {"succeeded": 0, "failed": 0, "skipped": 0}
    totals_lock = asyncio.Lock()

    limits = httpx.Limits(max_connections=max(16, args.workers * 2), max_keepalive_connections=16)
    async with httpx.AsyncClient(limits=limits, headers={"User-Agent": USER_AGENT}) as client:
        async def worker(index: int) -> None:
            worker_db = conn(args.db)
            worker_id = f"{socket.gethostname()}:{os.getpid()}:{index}:{uuid.uuid4().hex[:8]}"
            try:
                while True:
                    try:
                        accepted_url_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        job = claim_job(
                            worker_db,
                            accepted_url_id,
                            worker_id,
                            max_attempts=args.max_attempts,
                            lease_seconds=args.lease_seconds,
                        )
                        if job is None:
                            outcome = "skipped"
                        else:
                            succeeded = await process_job(
                                client,
                                worker_db,
                                job,
                                args.output_dir,
                                extractor,
                                fallback_lock,
                                force_opencli=args.force_opencli,
                                lease_seconds=args.lease_seconds,
                                max_attempts=args.max_attempts,
                            )
                            outcome = "succeeded" if succeeded else "failed"
                        async with totals_lock:
                            totals[outcome] += 1
                    finally:
                        queue.task_done()
            finally:
                worker_db.close()

        await asyncio.gather(*(worker(index) for index in range(args.workers)))

    summary_db = conn(args.db)
    try:
        status_counts = {
            row["status"]: row["count"]
            for row in summary_db.execute(
                "SELECT status, COUNT(*) AS count FROM downstream_downloads GROUP BY status"
            )
        }
    finally:
        summary_db.close()
    print(json.dumps({"run": totals, "database": status_counts}, sort_keys=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--max-repair-attempts",
        type=int,
        default=1,
        help="Bounded extra retry budgets granted when a downloaded artifact fails audit.",
    )
    parser.add_argument("--lease-seconds", type=int, default=900)
    parser.add_argument("--accepted-url-ids")
    parser.add_argument(
        "--opencli-host",
        default=os.getenv("VALUECHAIN_OPENCLI_HOST", "macmini-m4"),
        help="SSH host for browser fallback; empty disables it. Local OpenCLI is never used.",
    )
    parser.add_argument(
        "--opencli-profile",
        default=os.getenv("VALUECHAIN_OPENCLI_PROFILE", "auto-single"),
    )
    parser.add_argument(
        "--opencli-executable",
        default=os.getenv("VALUECHAIN_OPENCLI_EXECUTABLE", "/opt/homebrew/bin/opencli"),
    )
    parser.add_argument(
        "--opencli-helper",
        default=os.getenv(
            "VALUECHAIN_OPENCLI_HELPER",
            "/Users/frederickpi/.local/bin/valuechain-opencli-extract",
        ),
    )
    parser.add_argument(
        "--audit-existing",
        action="store_true",
        help="Verify v2 manifests/hashes/zstd and queue legacy or damaged artifacts for refetch.",
    )
    parser.add_argument(
        "--force-opencli",
        action="store_true",
        help="Development-only: bypass ULSCAR for web URLs and exercise the remote fallback.",
    )
    args = parser.parse_args()
    if (
        args.workers < 1
        or args.max_attempts < 1
        or args.max_repair_attempts < 0
        or args.lease_seconds < 30
    ):
        parser.error(
            "workers/max-attempts must be positive, max-repair-attempts nonnegative, "
            "and lease-seconds at least 30"
        )
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
