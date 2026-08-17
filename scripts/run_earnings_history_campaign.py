"""Run a resumable, DDG-first historical earnings-call campaign.

The runner accepts only a database created by ``init_earnings_history_campaign``.
It adds runtime-only tables to that database, never opens the Google/DDG A/B
study database, and never publishes to production Cosmos.  Search, download,
strict Pathfinder validation, and compressed artifact persistence are reused
from the existing production/study components.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_earnings_call_downstream import (
    ARTIFACT_SCHEMA_VERSION,
    Job,
    atomic_promote_verified_bundle,
    fetch_candidate,
    kind,
    read_zstd,
    verify_v2_artifact,
    write_bundle_manifest,
    zstd_is_valid,
)
from run_earnings_call_pathfinder import (
    VALIDATION_PROMPT_VERSION,
    blocked_downloader_host,
)
from run_earnings_call_pathfinder import (
    validate as validate_target_transcript,
)
from run_earnings_search_ab_study import (
    DDG_SERP_URL,
    DownloaderTrialRunner,
    DuckDuckGoBackend,
    SearchBackend,
    TrialOutcome,
    default_judge,
    normalize_url,
)

from valuechain.earnings_call_artifacts import compress_artifact_directory
from valuechain.earnings_call_content import (
    transcript_is_complete,
    transcript_quality_metrics,
)
from valuechain.earnings_calls import (
    USER_AGENT,
    Candidate,
    Judgement,
    apply_deterministic_candidate_rules,
    eligible,
)
from valuechain.remote_opencli import RemoteOpenCLIConfig, RemoteOpenCLIExtractor

SUPPORTED_CAMPAIGN_SCHEMA = 1
RUNTIME_SCHEMA_VERSION = 1
MAX_SEARCH_QUERIES = 4
DEFAULT_WORKERS = 8
TARGET_PERIOD_SEMANTICS_VERSION = "reported-fiscal-label-v1"
CANDIDATE_RETRY_BACKOFF_SECONDS = 30


@dataclass(frozen=True)
class QueryPolicyStep:
    ordinal: int
    engine: str
    variant: str


# This is deliberately the only place that decides engine/query order.  The
# A/B result can change the policy without changing the runner state machine.
QUERY_POLICY: tuple[QueryPolicyStep, ...] = (
    QueryPolicyStep(1, "duckduckgo", "canonical"),
    QueryPolicyStep(2, "duckduckgo", "youtube"),
    QueryPolicyStep(3, "duckduckgo", "transcript"),
    QueryPolicyStep(4, "duckduckgo", "results_conference"),
)
QUERY_POLICY_VERSION = "ddg-four-query-q1-to-q4-v1"


RUNTIME_SCHEMA = f"""
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS history_runtime (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  schema_version INTEGER NOT NULL,
  query_policy_version TEXT NOT NULL,
  target_period_semantics TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history_candidates (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  search_attempt_id INTEGER NOT NULL REFERENCES search_attempts(id),
  result_ordinal INTEGER NOT NULL CHECK (result_ordinal >= 0),
  query_ordinal INTEGER NOT NULL CHECK (query_ordinal BETWEEN 1 AND 4),
  engine TEXT NOT NULL CHECK (engine IN ('duckduckgo', 'google')),
  url TEXT NOT NULL,
  normalized_url TEXT NOT NULL,
  title TEXT NOT NULL,
  snippet TEXT NOT NULL,
  source_type TEXT NOT NULL,
  discovered_at TEXT NOT NULL,
  UNIQUE (search_attempt_id, result_ordinal)
);

CREATE TABLE IF NOT EXISTS history_judgements (
  candidate_id INTEGER PRIMARY KEY REFERENCES history_candidates(id),
  judgement_attempt_id INTEGER NOT NULL REFERENCES history_judgement_attempts(id),
  is_target INTEGER NOT NULL CHECK (is_target IN (0, 1)),
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  content_kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  judged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history_judgement_attempts (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  search_attempt_id INTEGER NOT NULL REFERENCES search_attempts(id),
  attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  error_category TEXT,
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE (search_attempt_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS history_trials (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES history_candidates(id),
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  candidate_rank INTEGER NOT NULL,
  attempt_no INTEGER NOT NULL CHECK (attempt_no >= 0),
  status TEXT NOT NULL CHECK (
    status IN (
      'running', 'retry_wait', 'download_failed', 'rejected', 'validated',
      'duplicate'
    )
  ),
  max_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_attempts > 0),
  next_attempt_at TEXT,
  fetch_method TEXT,
  text_chars INTEGER,
  artifact_path TEXT,
  validation_json TEXT,
  error_category TEXT,
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE (candidate_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS history_results (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  candidate_id INTEGER NOT NULL UNIQUE REFERENCES history_candidates(id),
  source_url TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  fetch_method TEXT,
  text_chars INTEGER,
  validation_json TEXT NOT NULL,
  completed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_history_candidates_job
  ON history_candidates(job_id, query_ordinal, result_ordinal);
CREATE INDEX IF NOT EXISTS ix_history_candidates_normalized
  ON history_candidates(job_id, normalized_url);
CREATE INDEX IF NOT EXISTS ix_history_trials_job
  ON history_trials(job_id, status, candidate_rank);
CREATE INDEX IF NOT EXISTS ix_history_trials_retry
  ON history_trials(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_history_judgement_attempts_search
  ON history_judgement_attempts(search_attempt_id, attempt_no);

INSERT OR IGNORE INTO history_runtime (
  id,schema_version,query_policy_version,target_period_semantics,created_at,updated_at
) VALUES (
  1,{RUNTIME_SCHEMA_VERSION},'{QUERY_POLICY_VERSION}',
  '{TARGET_PERIOD_SEMANTICS_VERSION}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
);
"""


REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "campaign": frozenset({"id", "schema_version", "paused"}),
    "companies": frozenset(
        {"cik", "ticker", "company_name", "cohort", "tier", "sector"}
    ),
    "jobs": frozenset(
        {
            "id",
            "cik",
            # The initializer retained these legacy names, but the runner
            # treats them exclusively as the company's reported target label
            # (for example MSFT FY26 Q1), never the call-date calendar bucket.
            "calendar_year",
            "quarter",
            "calendar_target",
            "fiscal_year",
            "fiscal_quarter",
            "period_end",
            "call_date",
            "reported_period_label",
            "priority",
            "stage",
            "status",
            "query_count",
            "attempt_count",
            "retry_count",
            "max_attempts",
            "next_attempt_at",
            "lease_owner",
            "lease_token",
            "lease_expires_at",
            "heartbeat_at",
            "last_error",
            "error_class",
        }
    ),
    "search_attempts": frozenset(
        {
            "id",
            "job_id",
            "ordinal",
            "engine",
            "query",
            "status",
            "result_count",
            "request_id",
            "started_at",
            "finished_at",
            "error",
        }
    ),
    "job_attempts": frozenset(
        {
            "job_id",
            "attempt_no",
            "worker_id",
            "lease_token",
            "stage",
            "status",
            "started_at",
            "finished_at",
            "error",
            "details_json",
        }
    ),
}


class CampaignRunnerError(RuntimeError):
    """Base class for history-runner failures."""


class CampaignSchemaError(CampaignRunnerError):
    """The supplied SQLite database is not an initializer campaign."""


class LostLeaseError(CampaignRunnerError):
    """A worker attempted to write after losing its fenced lease."""


class PermanentJobError(CampaignRunnerError):
    """A job cannot make progress through retrying infrastructure."""


class IncompleteSearchEvidenceError(PermanentJobError):
    """At least one bounded search request failed, so absence is unproven."""


class CampaignPausedError(CampaignRunnerError):
    """The campaign paused at a safe boundary after the current call."""


class RetryableJobError(CampaignRunnerError):
    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


@dataclass(frozen=True)
class ErrorDisposition:
    category: str
    retryable: bool
    detail: str


@dataclass(frozen=True)
class ProcessOutcome:
    status: str
    candidate_id: int | None = None
    artifact_path: str | None = None
    fetch_method: str | None = None
    text_chars: int | None = None
    validation: dict | None = None
    error: str | None = None


JudgeFunction = Callable[
    [str, int, str, list[Candidate]], Awaitable[list[Judgement]]
]
HistoryTrialFunction = Callable[
    [Mapping[str, object], sqlite3.Row, Judgement], Awaitable[TrialOutcome]
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def future(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _period_metadata(
    job: Mapping[str, object], validation: Mapping[str, object] | None = None
) -> dict[str, object | None]:
    """Resolve result-period metadata without treating call date as the target.

    ``calendar_year``/``quarter`` are legacy initializer column names.  They
    mean the company's reported target label (for example FY26 Q1), even when
    the actual call happened in calendar 2025.  Dates are descriptive metadata
    only and never participate in target-period rejection.
    """

    verdict = validation or {}
    target_year = int(job["calendar_year"])
    target_quarter = str(job["quarter"]).upper()

    raw_fiscal_year = verdict.get("fiscal_year") or job.get("fiscal_year")
    try:
        fiscal_year = int(raw_fiscal_year) if raw_fiscal_year is not None else target_year
    except (TypeError, ValueError):
        fiscal_year = target_year
    if not 1900 <= fiscal_year <= 2200:
        fiscal_year = target_year

    fiscal_quarter = str(
        verdict.get("fiscal_quarter")
        or job.get("fiscal_quarter")
        or target_quarter
    ).upper()
    if fiscal_quarter not in {"Q1", "Q2", "Q3", "Q4"}:
        fiscal_quarter = target_quarter

    def iso_date(key: str) -> str | None:
        raw = verdict.get(key) or job.get(key)
        if raw is None:
            return None
        value = str(raw).strip()
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
        return value

    reported_label = str(
        verdict.get("reported_period_label")
        or job.get("reported_period_label")
        or f"{target_year} {target_quarter}"
    ).strip()[:200]
    return {
        "target_year": target_year,
        "target_quarter": target_quarter,
        "target_period_label": str(job["calendar_target"]),
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "period_end": iso_date("period_end"),
        "call_date": iso_date("call_date"),
        "reported_period_label": reported_label,
    }


def query_policy(
    ticker: str, company_name: str, year: int, quarter: str
) -> tuple[tuple[QueryPolicyStep, str], ...]:
    """Build searches from the reported target label, not call-date quarter."""

    normalized_quarter = quarter.upper()
    if normalized_quarter not in {"Q1", "Q2", "Q3", "Q4"}:
        raise ValueError(f"invalid quarter: {quarter!r}")
    base = f"{ticker} {company_name} {year} {normalized_quarter}"
    variants = {
        "canonical": f"{base} earnings conference call",
        "youtube": f"{base} earnings conference call YouTube",
        "transcript": f"{base} earnings call transcript",
        "results_conference": f"{base} quarterly results conference call",
    }
    return tuple((step, variants[step.variant]) for step in QUERY_POLICY)


def _table_columns(db: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(row["name"] for row in db.execute(f"PRAGMA table_info({table})"))


def validate_campaign_schema(db: sqlite3.Connection) -> sqlite3.Row:
    tables = {
        row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing_tables = sorted(set(REQUIRED_SCHEMA) - tables)
    if missing_tables:
        raise CampaignSchemaError(
            f"not an earnings history campaign; missing tables: {missing_tables}"
        )
    for table, required in REQUIRED_SCHEMA.items():
        missing = sorted(required - _table_columns(db, table))
        if missing:
            raise CampaignSchemaError(f"campaign table {table} lacks columns: {missing}")
    row = db.execute("SELECT * FROM campaign WHERE id=1").fetchone()
    if row is None:
        raise CampaignSchemaError("campaign metadata row id=1 is missing")
    if int(row["schema_version"]) != SUPPORTED_CAMPAIGN_SCHEMA:
        raise CampaignSchemaError(
            f"unsupported campaign schema_version={row['schema_version']}"
        )
    return row


def connect_campaign(path: Path, *, add_runtime_schema: bool = True) -> sqlite3.Connection:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise CampaignSchemaError(f"campaign database does not exist: {path}") from exc
    db = sqlite3.connect(
        f"file:{resolved}?mode=rw", uri=True, timeout=60, isolation_level=None
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=60000")
    try:
        validate_campaign_schema(db)
        if add_runtime_schema:
            db.executescript(RUNTIME_SCHEMA)
            runtime = db.execute("SELECT * FROM history_runtime WHERE id=1").fetchone()
            if (
                runtime is None
                or int(runtime["schema_version"]) != RUNTIME_SCHEMA_VERSION
                or runtime["query_policy_version"] != QUERY_POLICY_VERSION
                or runtime["target_period_semantics"]
                != TARGET_PERIOD_SEMANTICS_VERSION
            ):
                raise CampaignSchemaError("history runtime schema/policy mismatch")
        return db
    except Exception:
        db.close()
        raise


def _job_query() -> str:
    return (
        "SELECT j.*,c.ticker,c.company_name,c.sector,c.cohort,c.tier "
        "FROM jobs j JOIN companies c ON c.cik=j.cik WHERE j.id=?"
    )


def _active_lease(
    db: sqlite3.Connection, job_id: int, lease_owner: str, lease_token: str
) -> sqlite3.Row:
    stamp = utc_now()
    row = db.execute(
        """
        SELECT * FROM jobs WHERE id=? AND status='running'
          AND lease_owner=? AND lease_token=? AND lease_expires_at>?
        """,
        (job_id, lease_owner, lease_token, stamp),
    ).fetchone()
    if row is None:
        raise LostLeaseError(f"job {job_id} lease is no longer owned by this worker")
    return row


def claim_job(
    db: sqlite3.Connection, worker_id: str, *, lease_seconds: int
) -> dict[str, object] | None:
    db.execute("BEGIN IMMEDIATE")
    try:
        campaign = db.execute("SELECT paused FROM campaign WHERE id=1").fetchone()
        if campaign is None or int(campaign["paused"]):
            db.execute("COMMIT")
            return None
        stamp = utc_now()
        row = db.execute(
            """
            SELECT id FROM jobs
            WHERE status IN ('pending','retry_wait')
              AND retry_count < max_attempts
              AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY priority,id LIMIT 1
            """,
            (stamp,),
        ).fetchone()
        if row is None:
            db.execute("COMMIT")
            return None
        job_id = int(row["id"])
        token = uuid.uuid4().hex
        attempt_no = db.execute(
            "SELECT attempt_count+1 FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        updated = db.execute(
            """
            UPDATE jobs SET status='running',attempt_count=?,lease_owner=?,
              lease_token=?,lease_acquired_at=?,lease_expires_at=?,heartbeat_at=?,
              next_attempt_at=NULL,last_started_at=?,updated_at=?
            WHERE id=? AND status IN ('pending','retry_wait')
            """,
            (
                attempt_no,
                worker_id,
                token,
                stamp,
                future(lease_seconds),
                stamp,
                stamp,
                stamp,
                job_id,
            ),
        ).rowcount
        if updated != 1:
            db.execute("ROLLBACK")
            return None
        stage = db.execute("SELECT stage FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        db.execute(
            """
            INSERT INTO job_attempts (
              job_id,attempt_no,worker_id,lease_token,stage,status,started_at
            ) VALUES (?,?,?,?,?,'started',?)
            """,
            (job_id, attempt_no, worker_id, token, stage, stamp),
        )
        claimed = dict(db.execute(_job_query(), (job_id,)).fetchone())
        db.execute("COMMIT")
        return claimed
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def heartbeat(
    db: sqlite3.Connection,
    job_id: int,
    lease_owner: str,
    lease_token: str,
    *,
    lease_seconds: int,
) -> None:
    stamp = utc_now()
    updated = db.execute(
        """
        UPDATE jobs SET heartbeat_at=?,lease_expires_at=?,updated_at=?
        WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
          AND lease_expires_at>?
        """,
        (
            stamp,
            future(lease_seconds),
            stamp,
            job_id,
            lease_owner,
            lease_token,
            stamp,
        ),
    ).rowcount
    if updated != 1:
        raise LostLeaseError(f"heartbeat lost fenced lease for job {job_id}")


def recover_stale_leases(db: sqlite3.Connection) -> int:
    stamp = utc_now()
    db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute(
            """
            SELECT id,attempt_count,retry_count,max_attempts FROM jobs
            WHERE status='running'
              AND (lease_expires_at IS NULL OR lease_expires_at<=?)
            """,
            (stamp,),
        ).fetchall()
        for row in rows:
            failures = int(row["retry_count"]) + 1
            retryable = failures < int(row["max_attempts"])
            status = "retry_wait" if retryable else "failed"
            db.execute(
                """
                UPDATE jobs SET status=?,retry_count=retry_count+1,
                  next_attempt_at=?,lease_owner=NULL,lease_token=NULL,
                  lease_acquired_at=NULL,lease_expires_at=NULL,heartbeat_at=NULL,
                  last_finished_at=?,last_error='stale worker lease recovered',
                  error_class='stale_lease',updated_at=? WHERE id=?
                """,
                (status, stamp if retryable else None, stamp, stamp, row["id"]),
            )
            db.execute(
                """
                UPDATE job_attempts SET status='expired',finished_at=?,
                  error='stale worker lease recovered'
                WHERE job_id=? AND attempt_no=? AND status='started'
                """,
                (stamp, row["id"], row["attempt_count"]),
            )
            db.execute(
                """
                UPDATE search_attempts SET status='failed',finished_at=?,
                  error='stale worker lease recovered'
                WHERE job_id=? AND status='running'
                """,
                (stamp, row["id"]),
            )
            db.execute(
                """
                UPDATE history_trials SET status='retry_wait',next_attempt_at=?,
                  error_category='stale_lease',error='stale worker lease recovered',
                  finished_at=?,updated_at=?
                WHERE job_id=? AND status='running'
                """,
                (stamp, stamp, stamp, row["id"]),
            )
            db.execute(
                """
                UPDATE history_judgement_attempts SET status='failed',
                  error_category='stale_lease',error='stale worker lease recovered',
                  finished_at=? WHERE job_id=? AND status='running'
                """,
                (stamp, row["id"]),
            )
        db.execute("COMMIT")
        return len(rows)
    except Exception:
        db.execute("ROLLBACK")
        raise


def set_campaign_paused(db: sqlite3.Connection, paused: bool) -> None:
    db.execute(
        "UPDATE campaign SET paused=?,updated_at=? WHERE id=1",
        (int(paused), utc_now()),
    )


def _check_not_paused(db: sqlite3.Connection) -> None:
    row = db.execute("SELECT paused FROM campaign WHERE id=1").fetchone()
    if row is None or int(row["paused"]):
        raise CampaignPausedError("campaign paused")


def classify_exception(exc: BaseException) -> ErrorDisposition:
    detail = f"{type(exc).__name__}: {exc}"[-4000:]
    if isinstance(exc, RetryableJobError):
        return ErrorDisposition(exc.category, True, exc.detail[-4000:])
    if isinstance(exc, IncompleteSearchEvidenceError):
        return ErrorDisposition("incomplete_search_evidence", False, detail)
    if isinstance(exc, (PermanentJobError, CampaignSchemaError)):
        return ErrorDisposition("permanent", False, detail)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retryable = status in {408, 409, 425, 429} or status >= 500
        return ErrorDisposition(f"http_{status}", retryable, detail)
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            OSError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    ):
        return ErrorDisposition("transient_infrastructure", True, detail)
    if isinstance(exc, (ValueError, TypeError)):
        return ErrorDisposition("invalid_content", False, detail)
    return ErrorDisposition("unexpected", True, detail)


def _clear_lease_sql() -> str:
    return (
        "lease_owner=NULL,lease_token=NULL,lease_acquired_at=NULL,"
        "lease_expires_at=NULL,heartbeat_at=NULL"
    )


def _finish_attempt(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    *,
    status: str,
    error: str | None,
    details: dict[str, object] | None = None,
) -> None:
    db.execute(
        """
        UPDATE job_attempts SET status=?,finished_at=?,error=?,details_json=?
        WHERE job_id=? AND attempt_no=? AND worker_id=? AND lease_token=?
          AND status='started'
        """,
        (
            status,
            utc_now(),
            error,
            json.dumps(details, sort_keys=True) if details else None,
            job["id"],
            job["attempt_count"],
            job["lease_owner"],
            job["lease_token"],
        ),
    )


def finish_completed(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    outcome: ProcessOutcome,
) -> None:
    if outcome.candidate_id is None or not outcome.artifact_path:
        raise ValueError("validated outcome lacks candidate/artifact")
    stamp = utc_now()
    db.execute("BEGIN IMMEDIATE")
    try:
        _active_lease(
            db,
            int(job["id"]),
            str(job["lease_owner"]),
            str(job["lease_token"]),
        )
        candidate = db.execute(
            "SELECT url FROM history_candidates WHERE id=? AND job_id=?",
            (outcome.candidate_id, job["id"]),
        ).fetchone()
        if candidate is None:
            raise PermanentJobError("winning candidate is absent from campaign database")
        validation_json = json.dumps(outcome.validation or {}, sort_keys=True)
        period = _period_metadata(job, outcome.validation)
        db.execute(
            """
            INSERT OR IGNORE INTO history_results (
              job_id,candidate_id,source_url,artifact_path,fetch_method,text_chars,
              validation_json,completed_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                job["id"],
                outcome.candidate_id,
                candidate["url"],
                outcome.artifact_path,
                outcome.fetch_method,
                outcome.text_chars,
                validation_json,
                stamp,
            ),
        )
        updated = db.execute(
            f"""
            UPDATE jobs SET status='completed',stage='done',completed_at=?,
              fiscal_year=?,fiscal_quarter=?,period_end=?,call_date=?,
              reported_period_label=?,
              next_attempt_at=NULL,last_finished_at=?,last_error=NULL,
              error_class=NULL,{_clear_lease_sql()},updated_at=?
            WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
            """,
            (
                stamp,
                period["fiscal_year"],
                period["fiscal_quarter"],
                period["period_end"],
                period["call_date"],
                period["reported_period_label"],
                stamp,
                stamp,
                job["id"],
                job["lease_owner"],
                job["lease_token"],
            ),
        ).rowcount
        if updated != 1:
            raise LostLeaseError(f"job {job['id']} lost lease before completion")
        _finish_attempt(
            db,
            job,
            status="succeeded",
            error=None,
            details={"candidate_id": outcome.candidate_id},
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def finish_exhausted(
    db: sqlite3.Connection, job: Mapping[str, object], error: str
) -> None:
    stamp = utc_now()
    db.execute("BEGIN IMMEDIATE")
    try:
        _active_lease(
            db,
            int(job["id"]),
            str(job["lease_owner"]),
            str(job["lease_token"]),
        )
        updated = db.execute(
            f"""
            UPDATE jobs SET status='exhausted',stage='done',next_attempt_at=NULL,
              last_finished_at=?,last_error=?,error_class='no_complete_transcript',
              {_clear_lease_sql()},updated_at=?
            WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
            """,
            (
                stamp,
                error[-4000:],
                stamp,
                job["id"],
                job["lease_owner"],
                job["lease_token"],
            ),
        ).rowcount
        if updated != 1:
            raise LostLeaseError(f"job {job['id']} lost lease before exhaustion")
        _finish_attempt(db, job, status="succeeded", error=error[-4000:])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def finish_failure(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    disposition: ErrorDisposition,
) -> str:
    stamp = utc_now()
    candidate_retry = disposition.retryable and disposition.category.startswith(
        "candidate_"
    )
    attempt_no = int(job["attempt_count"])
    max_attempts = int(job["max_attempts"])
    failures = int(job["retry_count"]) + 1
    retry = disposition.retryable and (
        candidate_retry or failures < max_attempts
    )
    status = "retry_wait" if retry else "failed"
    delay = min(3600, 30 * (2 ** max(0, attempt_no - 1)))
    next_attempt = future(delay) if retry else None
    db.execute("BEGIN IMMEDIATE")
    try:
        _active_lease(
            db,
            int(job["id"]),
            str(job["lease_owner"]),
            str(job["lease_token"]),
        )
        if candidate_retry:
            trial_due = db.execute(
                """
                SELECT MIN(next_attempt_at) FROM history_trials
                WHERE job_id=? AND status='retry_wait' AND next_attempt_at IS NOT NULL
                  AND attempt_no=(
                    SELECT MAX(newest.attempt_no) FROM history_trials AS newest
                    WHERE newest.candidate_id=history_trials.candidate_id
                  )
                """,
                (job["id"],),
            ).fetchone()[0]
            next_attempt = trial_due or future(CANDIDATE_RETRY_BACKOFF_SECONDS)
            updated = db.execute(
                f"""
                UPDATE jobs SET status='retry_wait',next_attempt_at=?,
                  last_finished_at=?,last_error=?,error_class=?,
                  {_clear_lease_sql()},updated_at=?
                WHERE id=? AND status='running'
                  AND lease_owner=? AND lease_token=?
                """,
                (
                    next_attempt,
                    stamp,
                    disposition.detail,
                    disposition.category,
                    stamp,
                    job["id"],
                    job["lease_owner"],
                    job["lease_token"],
                ),
            ).rowcount
            if updated != 1:
                raise LostLeaseError(
                    f"job {job['id']} lost lease before candidate retry commit"
                )
            _finish_attempt(
                db,
                job,
                status="failed",
                error=disposition.detail,
                details={"scope": "candidate", "job_retry_count_consumed": False},
            )
            db.execute("COMMIT")
            return "retry_wait"
        updated = db.execute(
            f"""
            UPDATE jobs SET status=?,retry_count=retry_count+1,next_attempt_at=?,
              last_finished_at=?,last_error=?,error_class=?,{_clear_lease_sql()},
              updated_at=? WHERE id=? AND status='running'
              AND lease_owner=? AND lease_token=?
            """,
            (
                status,
                next_attempt,
                stamp,
                disposition.detail,
                disposition.category,
                stamp,
                job["id"],
                job["lease_owner"],
                job["lease_token"],
            ),
        ).rowcount
        if updated != 1:
            raise LostLeaseError(f"job {job['id']} lost lease before failure commit")
        db.execute(
            """
            UPDATE search_attempts SET status='failed',finished_at=?,error=?
            WHERE job_id=? AND status='running'
            """,
            (stamp, disposition.detail, job["id"]),
        )
        db.execute(
            """
            UPDATE history_trials SET status=?,next_attempt_at=?,
              error_category=?,error=?,finished_at=?,updated_at=?
            WHERE job_id=? AND status='running'
            """,
            (
                "retry_wait" if retry else "download_failed",
                next_attempt,
                disposition.category,
                disposition.detail,
                stamp,
                stamp,
                job["id"],
            ),
        )
        db.execute(
            """
            UPDATE history_judgement_attempts SET status='failed',
              error_category=?,error=?,finished_at=?
            WHERE job_id=? AND status='running'
            """,
            (
                disposition.category,
                disposition.detail,
                stamp,
                job["id"],
            ),
        )
        _finish_attempt(
            db, job, status="failed", error=disposition.detail
        )
        db.execute("COMMIT")
        return status
    except Exception:
        db.execute("ROLLBACK")
        raise


def finish_interrupted(
    db: sqlite3.Connection, job: Mapping[str, object], reason: str
) -> None:
    stamp = utc_now()
    db.execute("BEGIN IMMEDIATE")
    try:
        _active_lease(
            db,
            int(job["id"]),
            str(job["lease_owner"]),
            str(job["lease_token"]),
        )
        updated = db.execute(
            f"""
            UPDATE jobs SET status='pending',next_attempt_at=NULL,
              last_finished_at=?,last_error=?,
              error_class=NULL,{_clear_lease_sql()},updated_at=?
            WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
            """,
            (
                stamp,
                reason[-4000:],
                stamp,
                job["id"],
                job["lease_owner"],
                job["lease_token"],
            ),
        ).rowcount
        if updated != 1:
            raise LostLeaseError(f"job {job['id']} lost lease during pause")
        _finish_attempt(
            db,
            job,
            status="cancelled",
            error=reason[-4000:],
        )
        db.execute(
            """
            UPDATE search_attempts SET status='failed',finished_at=?,error=?
            WHERE job_id=? AND status='running'
            """,
            (stamp, reason[-4000:], job["id"]),
        )
        db.execute(
            """
            UPDATE history_judgement_attempts SET status='failed',
              error_category='interrupted',error=?,finished_at=?
            WHERE job_id=? AND status='running'
            """,
            (reason[-4000:], stamp, job["id"]),
        )
        db.execute(
            """
            UPDATE history_trials SET status='retry_wait',next_attempt_at=?,
              error_category='interrupted',error=?,finished_at=?,updated_at=?
            WHERE job_id=? AND status='running'
            """,
            (stamp, reason[-4000:], stamp, stamp, job["id"]),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def _set_stage(
    db: sqlite3.Connection, job: Mapping[str, object], stage: str
) -> None:
    updated = db.execute(
        """
        UPDATE jobs SET stage=?,updated_at=? WHERE id=? AND status='running'
          AND lease_owner=? AND lease_token=?
        """,
        (stage, utc_now(), job["id"], job["lease_owner"], job["lease_token"]),
    ).rowcount
    if updated != 1:
        raise LostLeaseError(f"job {job['id']} lost lease while entering {stage}")


def _start_search_attempt(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    step: QueryPolicyStep,
    query: str,
) -> sqlite3.Row:
    existing = db.execute(
        "SELECT * FROM search_attempts WHERE job_id=? AND ordinal=?",
        (job["id"], step.ordinal),
    ).fetchone()
    if existing is not None:
        return existing
    stamp = utc_now()
    db.execute("BEGIN IMMEDIATE")
    try:
        active = _active_lease(
            db,
            int(job["id"]),
            str(job["lease_owner"]),
            str(job["lease_token"]),
        )
        if int(active["query_count"]) >= MAX_SEARCH_QUERIES:
            raise PermanentJobError("four-query campaign cap already reached")
        cursor = db.execute(
            """
            INSERT INTO search_attempts (
              job_id,ordinal,engine,query,status,result_count,started_at
            ) VALUES (?,?,?,?,'running',0,?)
            """,
            (job["id"], step.ordinal, step.engine, query, stamp),
        )
        db.execute(
            """
            UPDATE jobs SET query_count=query_count+1,stage='search',updated_at=?
            WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
              AND query_count<?
            """,
            (
                stamp,
                job["id"],
                job["lease_owner"],
                job["lease_token"],
                MAX_SEARCH_QUERIES,
            ),
        )
        row = db.execute(
            "SELECT * FROM search_attempts WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        db.execute("COMMIT")
        return row
    except Exception:
        db.execute("ROLLBACK")
        raise


def _candidate_rows(db: sqlite3.Connection, search_attempt_id: int) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM history_candidates WHERE search_attempt_id=?
        ORDER BY result_ordinal,id
        """,
        (search_attempt_id,),
    ).fetchall()


def _finish_search_success(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    search_attempt: sqlite3.Row,
    candidates: Sequence[Candidate],
    request_id: str | None,
) -> list[sqlite3.Row]:
    stamp = utc_now()
    db.execute("BEGIN IMMEDIATE")
    try:
        _active_lease(
            db,
            int(job["id"]),
            str(job["lease_owner"]),
            str(job["lease_token"]),
        )
        for result_ordinal, candidate in enumerate(candidates):
            raw_url = candidate.url.strip()
            if not raw_url:
                continue
            normalized = normalize_url(raw_url)
            db.execute(
                """
                INSERT OR IGNORE INTO history_candidates (
                  job_id,search_attempt_id,result_ordinal,query_ordinal,engine,
                  url,normalized_url,title,snippet,source_type,discovered_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job["id"],
                    search_attempt["id"],
                    result_ordinal,
                    search_attempt["ordinal"],
                    search_attempt["engine"],
                    raw_url,
                    normalized,
                    candidate.title,
                    candidate.snippet,
                    candidate.source_type,
                    stamp,
                ),
            )
        db.execute(
            """
            UPDATE search_attempts SET status='completed',result_count=?,
              request_id=?,finished_at=?,error=NULL WHERE id=? AND status='running'
            """,
            (len(candidates), request_id, stamp, search_attempt["id"]),
        )
        rows = _candidate_rows(db, int(search_attempt["id"]))
        db.execute("COMMIT")
        return rows
    except Exception:
        db.execute("ROLLBACK")
        raise


def _finish_search_failure(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    search_attempt: sqlite3.Row,
    exc: BaseException,
) -> None:
    disposition = classify_exception(exc)
    payload = json.dumps(
        {
            "category": disposition.category,
            "retryable": disposition.retryable,
            "detail": disposition.detail,
        },
        sort_keys=True,
    )
    updated = db.execute(
        """
        UPDATE search_attempts SET status='failed',finished_at=?,error=?
        WHERE id=? AND status='running' AND EXISTS (
          SELECT 1 FROM jobs WHERE id=? AND status='running'
            AND lease_owner=? AND lease_token=?
        )
        """,
        (
            utc_now(),
            payload,
            search_attempt["id"],
            job["id"],
            job["lease_owner"],
            job["lease_token"],
        ),
    ).rowcount
    if updated != 1:
        raise LostLeaseError(f"job {job['id']} lost lease after search failure")


async def ensure_search(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    step: QueryPolicyStep,
    query: str,
    backend: SearchBackend,
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    attempt = _start_search_attempt(db, job, step, query)
    if attempt["status"] == "completed":
        return attempt, _candidate_rows(db, int(attempt["id"]))
    if attempt["status"] == "failed":
        return attempt, []
    try:
        response = await backend.search(query, limit=10)
        rows = _finish_search_success(
            db, job, attempt, response.candidates, response.request_id
        )
        return db.execute(
            "SELECT * FROM search_attempts WHERE id=?", (attempt["id"],)
        ).fetchone(), rows
    except (LostLeaseError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 - each query failure is persisted
        _finish_search_failure(db, job, attempt, exc)
        return db.execute(
            "SELECT * FROM search_attempts WHERE id=?", (attempt["id"],)
        ).fetchone(), []


def _as_candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(
        url=str(row["url"]),
        title=str(row["title"]),
        snippet=str(row["snippet"]),
        engine=str(row["engine"]),
        query="",
        source_type=str(row["source_type"]),
    )


async def ensure_judgements(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    candidates: Sequence[sqlite3.Row],
    judge_fn: JudgeFunction,
) -> list[sqlite3.Row]:
    if not candidates:
        return []
    candidate_ids = [int(row["id"]) for row in candidates]
    placeholders = ",".join("?" for _ in candidate_ids)
    existing = db.execute(
        f"SELECT COUNT(*) FROM history_judgements WHERE candidate_id IN ({placeholders})",
        candidate_ids,
    ).fetchone()[0]
    if existing != len(candidate_ids):
        search_attempt_id = int(candidates[0]["search_attempt_id"])
        stamp = utc_now()
        db.execute("BEGIN IMMEDIATE")
        try:
            _active_lease(
                db,
                int(job["id"]),
                str(job["lease_owner"]),
                str(job["lease_token"]),
            )
            attempt_no = db.execute(
                """
                SELECT COALESCE(MAX(attempt_no),0)+1
                FROM history_judgement_attempts WHERE search_attempt_id=?
                """,
                (search_attempt_id,),
            ).fetchone()[0]
            judgement_attempt_id = db.execute(
                """
                INSERT INTO history_judgement_attempts (
                  job_id,search_attempt_id,attempt_no,status,started_at
                ) VALUES (?,?,?,'running',?)
                """,
                (job["id"], search_attempt_id, attempt_no, stamp),
            ).lastrowid
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

        target = f"{job['company_name']} ({job['ticker']})"
        model_candidates = [_as_candidate(row) for row in candidates]
        try:
            judgements = await judge_fn(
                target,
                int(job["calendar_year"]),
                str(job["quarter"]),
                model_candidates,
            )
            indexes = [judgement.candidate_index for judgement in judgements]
            expected = list(range(len(candidates)))
            if sorted(indexes) != expected or len(set(indexes)) != len(indexes):
                raise RetryableJobError(
                    "qwen_incomplete",
                    f"Qwen judgement indexes {indexes!r} did not cover {expected!r}",
                )
        except (LostLeaseError, asyncio.CancelledError):
            raise
        except Exception as exc:
            disposition = classify_exception(exc)
            db.execute(
                """
                UPDATE history_judgement_attempts SET status='failed',
                  error_category=?,error=?,finished_at=?
                WHERE id=? AND status='running'
                """,
                (
                    disposition.category,
                    disposition.detail,
                    utc_now(),
                    judgement_attempt_id,
                ),
            )
            if disposition.retryable:
                raise RetryableJobError(
                    disposition.category, disposition.detail
                ) from exc
            raise PermanentJobError(disposition.detail) from exc

        finished = utc_now()
        db.execute("BEGIN IMMEDIATE")
        try:
            _active_lease(
                db,
                int(job["id"]),
                str(job["lease_owner"]),
                str(job["lease_token"]),
            )
            for judgement in judgements:
                candidate_id = candidates[judgement.candidate_index]["id"]
                db.execute(
                    """
                    INSERT OR IGNORE INTO history_judgements (
                      candidate_id,judgement_attempt_id,is_target,confidence,
                      content_kind,reason,judged_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        candidate_id,
                        judgement_attempt_id,
                        int(judgement.is_target),
                        judgement.confidence,
                        judgement.content_kind,
                        judgement.reason,
                        finished,
                    ),
                )
            db.execute(
                """
                UPDATE history_judgement_attempts SET status='completed',finished_at=?
                WHERE id=? AND status='running'
                """,
                (finished, judgement_attempt_id),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    rows = db.execute(
        f"""
        SELECT c.*,j.is_target,j.confidence,j.content_kind,j.reason
        FROM history_candidates c
        JOIN history_judgements j ON j.candidate_id=c.id
        WHERE c.id IN ({placeholders})
        ORDER BY c.result_ordinal,c.id
        """,
        candidate_ids,
    ).fetchall()
    target = f"{job['company_name']} ({job['ticker']})"
    current = [
        Judgement(
            index,
            bool(row["is_target"]),
            float(row["confidence"]),
            str(row["content_kind"]),
            str(row["reason"]),
        )
        for index, row in enumerate(rows)
    ]
    effective = apply_deterministic_candidate_rules(
        target,
        int(job["calendar_year"]),
        str(job["quarter"]),
        [_as_candidate(row) for row in rows],
        current,
    )
    changes = [
        (row, prior, revised)
        for row, prior, revised in zip(rows, current, effective, strict=True)
        if (
            prior.is_target,
            prior.confidence,
            prior.content_kind,
        )
        != (
            revised.is_target,
            revised.confidence,
            revised.content_kind,
        )
    ]
    if changes:
        stamp = utc_now()
        db.execute("BEGIN IMMEDIATE")
        try:
            _active_lease(
                db,
                int(job["id"]),
                str(job["lease_owner"]),
                str(job["lease_token"]),
            )
            for row, prior, revised in changes:
                db.execute(
                    """
                    UPDATE history_judgements
                    SET is_target=?,confidence=?,content_kind=?,reason=?,judged_at=?
                    WHERE candidate_id=?
                    """,
                    (
                        int(revised.is_target),
                        revised.confidence,
                        revised.content_kind,
                        (
                            f"{revised.reason} Prior Qwen judgement: "
                            f"{prior.reason}"
                        )[:4000],
                        stamp,
                        row["id"],
                    ),
                )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        rows = db.execute(
            f"""
            SELECT c.*,j.is_target,j.confidence,j.content_kind,j.reason
            FROM history_candidates c
            JOIN history_judgements j ON j.candidate_id=c.id
            WHERE c.id IN ({placeholders})
            ORDER BY c.result_ordinal,c.id
            """,
            candidate_ids,
        ).fetchall()
    return rows


def _as_judgement(row: sqlite3.Row) -> Judgement:
    return Judgement(
        candidate_index=0,
        is_target=bool(row["is_target"]),
        confidence=float(row["confidence"]),
        content_kind=str(row["content_kind"]),
        reason=str(row["reason"]),
    )


def _eligible_candidates(rows: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
    content_priority = {
        "official_transcript": 0,
        "third_party_transcript": 1,
        "official_webcast": 2,
        "youtube_video": 3,
    }
    accepted = [
        row
        for row in rows
        if eligible(_as_candidate(row), _as_judgement(row))
    ]
    return sorted(
        accepted,
        key=lambda row: (
            content_priority.get(str(row["content_kind"]), 9),
            -float(row["confidence"]),
            int(row["result_ordinal"]),
            int(row["id"]),
        ),
    )


def _existing_trial(db: sqlite3.Connection, candidate_id: int) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT * FROM history_trials WHERE candidate_id=?
        ORDER BY attempt_no DESC,id DESC LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()


def _validated_outcome(row: sqlite3.Row) -> ProcessOutcome:
    validation = json.loads(row["validation_json"] or "{}")
    return ProcessOutcome(
        "validated",
        candidate_id=int(row["candidate_id"]),
        artifact_path=str(row["artifact_path"] or ""),
        fetch_method=row["fetch_method"],
        text_chars=row["text_chars"],
        validation=validation,
    )


def rejected_artifact_matches_current_gate(
    trial: Mapping[str, object],
    candidate: Mapping[str, object],
    judgement: Judgement,
) -> bool:
    """Detect a prior false negative after deterministic gates improve.

    A rejected immutable version is never modified or promoted in place.  If
    its verified transcript passes the *current* gate, ``ensure_trial`` grants
    the candidate one normal new attempt so the downloader can create a clean
    version whose metadata and manifest reflect the current decision.
    """
    try:
        artifact_path = trial["artifact_path"]
        validation_json = trial["validation_json"]
        if not artifact_path or not validation_json:
            return False
        verified = verify_v2_artifact(
            Path(str(artifact_path)).parent,
            expected_accepted_url_id=int(candidate["id"]),
        )
        if verified is None:
            return False
        transcript_path, metadata = verified
        verdict = json.loads(str(validation_json))
        if not isinstance(verdict, dict) or metadata.get("validation") != verdict:
            return False
        text = read_zstd(transcript_path).decode("utf-8")
        return transcript_is_complete(
            verdict,
            text,
            source_kind=kind(str(candidate["url"])),
            content_kind=judgement.content_kind,
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, OSError):
        return False


async def ensure_trial(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    candidate: sqlite3.Row,
    judgement: Judgement,
    trial_fn: HistoryTrialFunction,
    *,
    candidate_rank: int,
    max_candidate_attempts: int,
) -> ProcessOutcome | None:
    existing = _existing_trial(db, int(candidate["id"]))
    if existing is not None and existing["status"] == "validated":
        return _validated_outcome(existing)
    revalidate_rejected = bool(
        existing is not None
        and existing["status"] == "rejected"
        and rejected_artifact_matches_current_gate(existing, candidate, judgement)
    )
    if existing is not None and existing["status"] in {
        "rejected",
        "download_failed",
        "duplicate",
    } and not revalidate_rejected:
        return None
    if (
        existing is not None
        and existing["status"] == "retry_wait"
        and existing["next_attempt_at"]
        and existing["next_attempt_at"] > utc_now()
    ):
        raise RetryableJobError("candidate_backoff", "candidate retry is not due")

    # Duplicate suppression is only a first-attempt decision.  Applying it to
    # an existing candidate retry (or a gate-policy revalidation) can create a
    # reverse duplicate cycle where A points to B and B already points to A.
    duplicate = None
    if existing is None:
        duplicate = db.execute(
            """
            SELECT t.* FROM history_trials t
            JOIN history_candidates c ON c.id=t.candidate_id
            WHERE t.job_id=? AND c.normalized_url=? AND c.id<>?
              AND t.status IN ('rejected','download_failed','duplicate')
            ORDER BY t.candidate_rank LIMIT 1
            """,
            (job["id"], candidate["normalized_url"], candidate["id"]),
        ).fetchone()
    if duplicate is not None:
        db.execute(
            """
            INSERT OR IGNORE INTO history_trials (
              candidate_id,job_id,candidate_rank,attempt_no,status,max_attempts,
              error_category,error,finished_at,updated_at
            ) VALUES (?,?,?,0,'duplicate',?,'duplicate_url',?,?,?)
            """,
            (
                candidate["id"],
                job["id"],
                candidate_rank,
                max_candidate_attempts,
                f"same normalized URL as candidate {duplicate['candidate_id']}",
                utc_now(),
                utc_now(),
            ),
        )
        return None

    attempt_no = int(existing["attempt_no"] if existing else 0) + 1
    if attempt_no > max_candidate_attempts:
        return None
    stamp = utc_now()
    db.execute("BEGIN IMMEDIATE")
    try:
        _active_lease(
            db,
            int(job["id"]),
            str(job["lease_owner"]),
            str(job["lease_token"]),
        )
        trial_id = db.execute(
            """
            INSERT INTO history_trials (
              candidate_id,job_id,candidate_rank,attempt_no,status,max_attempts,
              started_at,updated_at
            ) VALUES (?,?,?,?,'running',?,?,?)
            """,
            (
                candidate["id"],
                job["id"],
                candidate_rank,
                attempt_no,
                max_candidate_attempts,
                stamp,
                stamp,
            ),
        ).lastrowid
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    _set_stage(db, job, "download")
    try:
        outcome = await trial_fn(job, candidate, judgement)
        if outcome.status not in {"validated", "rejected", "download_failed"}:
            raise ValueError(f"unsupported trial outcome: {outcome.status!r}")
        if outcome.status == "validated" and not outcome.artifact_path:
            raise ValueError("validated trial did not return a compressed artifact")
        finished = utc_now()
        db.execute("BEGIN IMMEDIATE")
        try:
            _active_lease(
                db,
                int(job["id"]),
                str(job["lease_owner"]),
                str(job["lease_token"]),
            )
            db.execute(
                """
                UPDATE history_trials SET status=?,next_attempt_at=NULL,
                  fetch_method=?,text_chars=?,artifact_path=?,validation_json=?,
                  error_category=NULL,error=?,finished_at=?,updated_at=?
                WHERE id=? AND status='running'
                """,
                (
                    outcome.status,
                    outcome.fetch_method,
                    outcome.text_chars,
                    outcome.artifact_path,
                    json.dumps(outcome.validation or {}, sort_keys=True),
                    outcome.error,
                    finished,
                    finished,
                    trial_id,
                ),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        if outcome.status == "validated":
            return ProcessOutcome(
                "validated",
                candidate_id=int(candidate["id"]),
                artifact_path=outcome.artifact_path,
                fetch_method=outcome.fetch_method,
                text_chars=outcome.text_chars,
                validation=outcome.validation or {},
            )
        return None
    except (LostLeaseError, asyncio.CancelledError):
        raise
    except Exception as exc:
        disposition = classify_exception(exc)
        retry = disposition.retryable and attempt_no < max_candidate_attempts
        finished = utc_now()
        candidate_category = (
            disposition.category
            if disposition.category.startswith("candidate_")
            else f"candidate_{disposition.category}"
        )
        candidate_due = (
            future(CANDIDATE_RETRY_BACKOFF_SECONDS) if retry else None
        )
        db.execute(
            """
            UPDATE history_trials SET status=?,next_attempt_at=?,error_category=?,
              error=?,finished_at=?,updated_at=? WHERE id=?
              AND status='running' AND EXISTS (
                SELECT 1 FROM jobs WHERE id=? AND status='running'
                  AND lease_owner=? AND lease_token=?
              )
            """,
            (
                "retry_wait" if retry else "download_failed",
                candidate_due,
                candidate_category,
                disposition.detail,
                finished,
                finished,
                trial_id,
                job["id"],
                job["lease_owner"],
                job["lease_token"],
            ),
        )
        if retry:
            raise RetryableJobError(candidate_category, disposition.detail) from exc
        return None


async def process_job(
    db: sqlite3.Connection,
    job: Mapping[str, object],
    *,
    backends: Mapping[str, SearchBackend],
    judge_fn: JudgeFunction,
    trial_fn: HistoryTrialFunction,
    max_candidate_attempts: int,
) -> ProcessOutcome:
    _check_not_paused(db)
    prior_result = db.execute(
        """
        SELECT r.*,t.status FROM history_results r
        JOIN history_trials t ON t.candidate_id=r.candidate_id
        WHERE r.job_id=? AND t.status='validated'
        """,
        (job["id"],),
    ).fetchone()
    if prior_result is not None:
        return ProcessOutcome(
            "validated",
            candidate_id=int(prior_result["candidate_id"]),
            artifact_path=str(prior_result["artifact_path"]),
            fetch_method=prior_result["fetch_method"],
            text_chars=prior_result["text_chars"],
            validation=json.loads(prior_result["validation_json"]),
        )

    candidate_rank = 0
    for step, query in query_policy(
        str(job["ticker"]),
        str(job["company_name"]),
        int(job["calendar_year"]),
        str(job["quarter"]),
    ):
        _check_not_paused(db)
        backend = backends.get(step.engine)
        if backend is None:
            raise PermanentJobError(f"missing configured search backend {step.engine}")
        search_attempt, candidates = await ensure_search(
            db, job, step, query, backend
        )
        if search_attempt["status"] != "completed":
            continue
        _check_not_paused(db)
        judged = await ensure_judgements(db, job, candidates, judge_fn)
        for candidate in _eligible_candidates(judged):
            _check_not_paused(db)
            candidate_rank += 1
            outcome = await ensure_trial(
                db,
                job,
                candidate,
                _as_judgement(candidate),
                trial_fn,
                candidate_rank=candidate_rank,
                max_candidate_attempts=max_candidate_attempts,
            )
            if outcome is not None and outcome.status == "validated":
                return outcome

    attempts = db.execute(
        "SELECT status,COUNT(*) AS count FROM search_attempts WHERE job_id=? GROUP BY status",
        (job["id"],),
    ).fetchall()
    counts = {row["status"]: row["count"] for row in attempts}
    failed_searches = int(counts.get("failed", 0))
    if failed_searches:
        raise IncompleteSearchEvidenceError(
            f"{failed_searches} of four bounded search requests failed; "
            "no complete transcript can be inferred from incomplete evidence"
        )
    return ProcessOutcome(
        "exhausted", error="four-query Pathfinder found no strict complete transcript"
    )


async def _heartbeat_loop(
    db_path: Path,
    job: Mapping[str, object],
    lease_seconds: int,
) -> None:
    heartbeat_db = connect_campaign(db_path, add_runtime_schema=False)
    try:
        interval = max(10, min(60, lease_seconds // 3))
        while True:
            await asyncio.sleep(interval)
            heartbeat(
                heartbeat_db,
                int(job["id"]),
                str(job["lease_owner"]),
                str(job["lease_token"]),
                lease_seconds=lease_seconds,
            )
    finally:
        heartbeat_db.close()


async def run_workers(
    db_path: Path,
    *,
    backends: Mapping[str, SearchBackend],
    trial_fn: HistoryTrialFunction,
    judge_fn: JudgeFunction = default_judge,
    workers: int = DEFAULT_WORKERS,
    limit: int | None = None,
    lease_seconds: int = 900,
    max_candidate_attempts: int = 2,
) -> dict[str, object]:
    if workers < 1 or lease_seconds < 30 or max_candidate_attempts < 1:
        raise ValueError("workers/attempts must be positive and lease_seconds >= 30")
    setup = connect_campaign(db_path)
    try:
        recovered = recover_stale_leases(setup)
    finally:
        setup.close()

    claimed = 0
    claim_lock = asyncio.Lock()
    totals = {
        "completed": 0,
        "exhausted": 0,
        "retry_wait": 0,
        "failed": 0,
        "lost_lease": 0,
        "paused": 0,
    }
    totals_lock = asyncio.Lock()

    async def worker(index: int) -> None:
        nonlocal claimed
        db = connect_campaign(db_path, add_runtime_schema=False)
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{index}:{uuid.uuid4().hex[:8]}"
        try:
            while True:
                async with claim_lock:
                    if limit is not None and claimed >= limit:
                        return
                    job = claim_job(db, worker_id, lease_seconds=lease_seconds)
                    if job is None:
                        return
                    claimed += 1
                heartbeater = asyncio.create_task(
                    _heartbeat_loop(db_path, job, lease_seconds)
                )
                outcome_name = "failed"
                try:
                    outcome = await process_job(
                        db,
                        job,
                        backends=backends,
                        judge_fn=judge_fn,
                        trial_fn=trial_fn,
                        max_candidate_attempts=max_candidate_attempts,
                    )
                    if outcome.status == "validated":
                        finish_completed(db, job, outcome)
                        outcome_name = "completed"
                    else:
                        finish_exhausted(db, job, outcome.error or "exhausted")
                        outcome_name = "exhausted"
                except asyncio.CancelledError as exc:
                    finish_interrupted(
                        db, job, f"worker cancelled: {type(exc).__name__}: {exc}"
                    )
                    outcome_name = "paused"
                    raise
                except CampaignPausedError:
                    finish_interrupted(db, job, "campaign paused at safe boundary")
                    outcome_name = "paused"
                except LostLeaseError:
                    outcome_name = "lost_lease"
                except Exception as exc:  # noqa: BLE001 - worker boundary persists errors
                    outcome_name = finish_failure(db, job, classify_exception(exc))
                finally:
                    heartbeater.cancel()
                    await asyncio.gather(heartbeater, return_exceptions=True)
                    async with totals_lock:
                        totals[outcome_name] += 1
        finally:
            db.close()

    await asyncio.gather(*(worker(index) for index in range(workers)))
    return {"recovered_stale_leases": recovered, "claimed": claimed, **totals}


class CampaignDownloaderTrialRunner(DownloaderTrialRunner):
    """A/B downloader logic with campaign-specific compressed metadata."""

    def __init__(self, *args, campaign_db: Path, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.campaign_db = campaign_db

    def _enrich_period_verdict(self, verdict: Mapping[str, object]) -> dict:
        enriched = dict(verdict)
        # The model validates the reported target label.  These fields make
        # that semantic explicit downstream; call_date/period_end remain null
        # unless the validator or source metadata actually supplies them.
        # Once period_match is true, the campaign target is authoritative.
        # A model can otherwise mistake a fiscal Q2 FY26 call described as the
        # "December 2025 quarter" for fiscal year 2025.
        enriched["fiscal_year"] = self.year
        enriched["fiscal_quarter"] = self.quarter
        enriched.setdefault("reported_period_label", f"{self.year} {self.quarter}")
        enriched["target_period_semantics"] = TARGET_PERIOD_SEMANTICS_VERSION
        return enriched

    async def __call__(
        self,
        company: Mapping[str, object],
        candidate: sqlite3.Row,
        judgement: Judgement,
    ) -> TrialOutcome:
        """Download and validate against the result label, not the call date.

        The A/B study's cheap URL-year guard assumes a calendar target.  That
        is unsafe for this history campaign: a ``2025`` URL can legitimately
        contain MSFT's FY26 Q1 call.  History therefore delegates exact period
        matching to the transcript validator and never rejects from call-date
        calendar year/quarter alone.
        """

        url = str(candidate["url"])
        source_kind = kind(url)
        if blocked_downloader_host(url):
            return TrialOutcome(
                "download_failed",
                error=(
                    "source domain is blacklisted after repeated "
                    "unreadable/paywall downloads"
                ),
            )

        target = (
            self.artifact_root
            / str(company["ticker"])
            / str(candidate["engine"])
            / f"q{candidate['query_ordinal']}"
            / str(candidate["id"])
        )
        recovered = self._recover_artifact(
            target, source_kind, judgement.content_kind
        )
        if recovered is not None:
            return recovered

        staging_root = self.artifact_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=staging_root,
            prefix=(
                f"{company['ticker']}-{candidate['engine']}-"
                f"{candidate['id']}-"
            ),
        ) as temporary:
            staging = Path(temporary)
            download_job = Job(
                accepted_url_id=int(candidate["id"]),
                url=url,
                source_kind=source_kind,
                attempt_no=1,
                worker_id="earnings-history",
            )
            text, method, quality, primary_error = await fetch_candidate(
                self.client,
                download_job,
                staging,
                self.extractor,
                self.fallback_lock,
                force_opencli=False,
            )
            if len(text) < 2_500:
                raise RuntimeError(f"cleaned transcript too short: {len(text)}")
            company_target = f"{company['company_name']} ({company['ticker']})"
            verdict = self._enrich_period_verdict(
                await validate_target_transcript(
                    company_target, self.year, self.quarter, text
                )
            )
            complete = transcript_is_complete(
                verdict,
                text,
                source_kind=source_kind,
                content_kind=judgement.content_kind,
            )
            if (
                source_kind == "web"
                and not complete
                and not method.startswith("opencli.")
                and verdict.get("document_type")
                in {"earnings_call", "partial_call"}
                and self.extractor is not None
            ):
                try:
                    browser_text, browser_method, browser_quality, browser_error = (
                        await fetch_candidate(
                            self.client,
                            download_job,
                            staging,
                            self.extractor,
                            self.fallback_lock,
                            force_opencli=True,
                            opencli_reason=(
                                "strict transcript gate rejected ULSCAR extraction"
                            ),
                        )
                    )
                    if len(browser_text) > len(text) + 1_000:
                        text = browser_text
                        method = browser_method
                        quality = browser_quality
                        primary_error = browser_error or primary_error
                        verdict = self._enrich_period_verdict(
                            await validate_target_transcript(
                                company_target, self.year, self.quarter, text
                            )
                        )
                        complete = transcript_is_complete(
                            verdict,
                            text,
                            source_kind=source_kind,
                            content_kind=judgement.content_kind,
                        )
                except Exception as exc:  # noqa: BLE001 - fallback is best effort
                    primary_error = (
                        f"{primary_error or ''}; remote OpenCLI retry: "
                        f"{type(exc).__name__}: {exc}"
                    ).strip("; ")

            artifact = await asyncio.to_thread(
                self._persist_artifact,
                staging,
                target,
                company,
                candidate,
                judgement,
                text,
                method,
                quality,
                primary_error,
                verdict,
                complete,
            )
            return TrialOutcome(
                "validated" if complete else "rejected",
                fetch_method=method,
                text_chars=len(text),
                artifact_path=str(artifact),
                validation=verdict,
            )

    def _persist_artifact(
        self,
        staging: Path,
        target: Path,
        company: Mapping[str, object],
        candidate: sqlite3.Row,
        judgement: Judgement,
        text: str,
        method: str,
        quality: dict,
        primary_error: str | None,
        verdict: dict,
        complete: bool,
    ) -> Path:
        text_bytes = text.encode("utf-8")
        digest = hashlib.sha256(text_bytes).hexdigest()
        transcript = staging / "transcript.txt"
        transcript.write_bytes(text_bytes)
        period = _period_metadata(company, verdict)
        metadata = {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "dataset": "valuechain-earnings-history",
            "cik": company["cik"],
            "ticker": company["ticker"],
            "company_name": company["company_name"],
            "target_period_semantics": TARGET_PERIOD_SEMANTICS_VERSION,
            "target_year": period["target_year"],
            "target_quarter": period["target_quarter"],
            "target_period_label": period["target_period_label"],
            "year": period["target_year"],
            # Retained as compatibility aliases for the initializer schema.
            "calendar_year": company["calendar_year"],
            "quarter": company["quarter"],
            "calendar_target": company["calendar_target"],
            "fiscal_year": period["fiscal_year"],
            "fiscal_quarter": period["fiscal_quarter"],
            "period_end": period["period_end"],
            "call_date": period["call_date"],
            "reported_period_label": period["reported_period_label"],
            "query_policy_version": QUERY_POLICY_VERSION,
            "validation_prompt_version": VALIDATION_PROMPT_VERSION,
            "engine": candidate["engine"],
            "query_ordinal": candidate["query_ordinal"],
            "candidate_id": candidate["id"],
            "accepted_url_id": candidate["id"],
            "source_url": candidate["url"],
            "source_kind": kind(candidate["url"]),
            "content_kind": judgement.content_kind,
            "fetch_method": method,
            "fetched_at": utc_now(),
            "text_chars": len(text),
            "text_sha256": digest,
            "quality": quality or transcript_quality_metrics(text),
            "primary_error": primary_error,
            "validation": verdict,
            "strict_complete": complete,
        }
        (staging / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        compressed = compress_artifact_directory(staging)
        write_bundle_manifest(staging, int(candidate["id"]))
        compress_artifact_directory(staging)
        for path in staging.iterdir():
            if path.is_file() and path.suffix == ".zst" and not zstd_is_valid(path):
                raise RuntimeError(f"zstd integrity check failed: {path.name}")
        if verify_v2_artifact(staging) is None:
            raise RuntimeError("history staging artifact failed manifest/hash/zstd audit")

        # A token-scoped immutable version prevents an old worker from
        # overwriting a newer winner.  The one filesystem-visible operation is
        # a same-filesystem directory rename performed while the campaign's
        # fenced lease is rechecked under BEGIN IMMEDIATE.  A DB commit failure
        # can leave only a complete orphan version, never a partial target.
        version = (
            target
            / "versions"
            / f"lease-{str(company['lease_token'])[:16]}-{uuid.uuid4().hex[:12]}"
        )
        lease_db = connect_campaign(self.campaign_db, add_runtime_schema=False)
        try:
            lease_db.execute("BEGIN IMMEDIATE")
            _active_lease(
                lease_db,
                int(company["id"]),
                str(company["lease_owner"]),
                str(company["lease_token"]),
            )
            promoted = atomic_promote_verified_bundle(staging, version)
            lease_db.execute("COMMIT")
        except Exception:
            if lease_db.in_transaction:
                lease_db.execute("ROLLBACK")
            raise
        finally:
            lease_db.close()
        artifact = promoted / compressed[transcript].name
        if verify_v2_artifact(promoted) is None:
            raise RuntimeError("atomically promoted history artifact failed audit")
        return artifact


class HistoryTrialRunner:
    def __init__(
        self,
        *,
        campaign_db: Path,
        client: httpx.AsyncClient,
        artifact_root: Path,
        extractor: RemoteOpenCLIExtractor | None,
        fallback_lock: asyncio.Lock,
    ) -> None:
        self.campaign_db = campaign_db
        self.client = client
        self.artifact_root = artifact_root
        self.extractor = extractor
        self.fallback_lock = fallback_lock

    async def __call__(
        self,
        job: Mapping[str, object],
        candidate: sqlite3.Row,
        judgement: Judgement,
    ) -> TrialOutcome:
        root = (
            self.artifact_root
            / str(job["cik"])
            / str(job["calendar_target"])
        )
        runner = CampaignDownloaderTrialRunner(
            campaign_db=self.campaign_db,
            client=self.client,
            artifact_root=root,
            year=int(job["calendar_year"]),
            quarter=str(job["quarter"]),
            extractor=self.extractor,
            fallback_lock=self.fallback_lock,
        )
        return await runner(job, candidate, judgement)


def build_report(db: sqlite3.Connection) -> dict[str, object]:
    campaign = validate_campaign_schema(db)
    runtime_tables = {
        row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    has_runtime = "history_runtime" in runtime_tables
    statuses = {
        row["status"]: row["count"]
        for row in db.execute(
            "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status"
        )
    }
    report: dict[str, object] = {
        "campaign_mode": campaign["mode"],
        "paused": bool(campaign["paused"]),
        "jobs": sum(statuses.values()),
        "job_status": statuses,
        "query_count": db.execute("SELECT COALESCE(SUM(query_count),0) FROM jobs").fetchone()[0],
        "max_job_query_count": db.execute("SELECT COALESCE(MAX(query_count),0) FROM jobs").fetchone()[0],
        "runtime_initialized": has_runtime,
    }
    if has_runtime:
        report.update(
            {
                "search_attempts": db.execute(
                    "SELECT COUNT(*) FROM search_attempts"
                ).fetchone()[0],
                "candidate_links": db.execute(
                    "SELECT COUNT(*) FROM history_candidates"
                ).fetchone()[0],
                "judgements": db.execute(
                    "SELECT COUNT(*) FROM history_judgements"
                ).fetchone()[0],
                "trials": db.execute("SELECT COUNT(*) FROM history_trials").fetchone()[0],
                "validated_results": db.execute(
                    "SELECT COUNT(*) FROM history_results"
                ).fetchone()[0],
            }
        )
    return report


async def main_async(args: argparse.Namespace) -> None:
    setup = connect_campaign(args.db)
    try:
        recovered = recover_stale_leases(setup)
        print(json.dumps({"recovered_stale_leases": recovered}), flush=True)
    finally:
        setup.close()

    limits = httpx.Limits(
        max_connections=max(24, args.workers * 3), max_keepalive_connections=16
    )
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, limits=limits, follow_redirects=True
    ) as client:
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
        trial_runner = HistoryTrialRunner(
            campaign_db=args.db,
            client=client,
            artifact_root=args.artifact_dir,
            extractor=extractor,
            fallback_lock=asyncio.Lock(),
        )
        result = await run_workers(
            args.db,
            backends={
                "duckduckgo": DuckDuckGoBackend(client, args.ddg_url),
            },
            trial_fn=trial_runner,
            workers=args.workers,
            limit=args.limit,
            lease_seconds=args.lease_seconds,
            max_candidate_attempts=args.max_candidate_attempts,
        )
    report_db = connect_campaign(args.db)
    try:
        print(json.dumps({"run": result, "campaign": build_report(report_db)}, indent=2, sort_keys=True))
    finally:
        report_db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--lease-seconds", type=int, default=900)
    parser.add_argument("--max-candidate-attempts", type=int, default=2)
    parser.add_argument("--ddg-url", default=DDG_SERP_URL)
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--pause", action="store_true")
    controls.add_argument("--resume", action="store_true")
    controls.add_argument("--report-only", action="store_true")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Reserved; production Cosmos publishing is intentionally disconnected.",
    )
    parser.add_argument(
        "--opencli-host", default=os.getenv("VALUECHAIN_OPENCLI_HOST", "macmini-m4")
    )
    parser.add_argument(
        "--opencli-profile", default=os.getenv("VALUECHAIN_OPENCLI_PROFILE", "auto-single")
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
    args = parser.parse_args()
    if args.workers < 1 or args.lease_seconds < 30 or args.max_candidate_attempts < 1:
        parser.error("workers/attempts must be positive and lease-seconds >= 30")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.publish:
        parser.error("--publish is intentionally disconnected from production Cosmos")
    if not (args.pause or args.resume or args.report_only) and args.artifact_dir is None:
        parser.error("--artifact-dir is required for a run")
    return args


def main() -> None:
    args = parse_args()
    if args.pause or args.resume or args.report_only:
        db = connect_campaign(args.db)
        try:
            if args.pause:
                set_campaign_paused(db, True)
            elif args.resume:
                set_campaign_paused(db, False)
            print(json.dumps(build_report(db), indent=2, sort_keys=True))
        finally:
            db.close()
        return
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
