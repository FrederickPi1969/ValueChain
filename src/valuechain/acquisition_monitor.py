from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Literal, Sequence

import httpx
import psycopg
from psycopg.rows import dict_row


Status = Literal["ok", "warning", "critical"]
STATUS_RANK: dict[Status, int] = {"ok": 0, "warning": 1, "critical": 2}

DEFAULT_SOURCES = (
    "sec_edgar",
    "cninfo",
    "priority_eu_esef",
    "opendart",
    "edinet",
    "twse",
    "tpex",
    "companies_house_accounts_bulk",
    "cvm_brazil",
)
DEFAULT_SERVICES = (
    "valuechain-sec-acquisition.service",
    "valuechain-cninfo-acquisition.service",
    "valuechain-esef-acquisition.service",
    "valuechain-opendart-acquisition.service",
    "valuechain-edinet-acquisition.service",
    "valuechain-twse-acquisition.service",
    "valuechain-tpex-acquisition.service",
    "valuechain-companies-house-bulk-acquisition.service",
    "valuechain-cvm-bulk-acquisition.service",
    "valuechain-ad-hoc-acquisition.service",
)


@dataclass(frozen=True)
class MonitorConfig:
    database_url: str
    storage_path: Path
    report_dir: Path
    sources: tuple[str, ...] = DEFAULT_SOURCES
    services: tuple[str, ...] = DEFAULT_SERVICES
    stale_warning_minutes: int = 30
    stale_critical_minutes: int = 90
    stuck_claim_minutes: int = 30
    disk_warning_free_percent: float = 5.0
    disk_critical_free_percent: float = 2.0
    file_sample_size: int = 25
    webhook_url: str = ""
    webhook_repeat_hours: int = 6

    @classmethod
    def from_env(cls) -> MonitorConfig:
        database_url = os.getenv("VALUECHAIN_ACQUISITION_DATABASE_URL") or os.getenv(
            "VALUECHAIN_DATABASE_URL", ""
        )
        if not database_url:
            raise ValueError(
                "VALUECHAIN_ACQUISITION_DATABASE_URL or VALUECHAIN_DATABASE_URL is required"
            )
        reports_root = Path(os.getenv("VALUECHAIN_REPORTS_DIR", "data/reports"))
        return cls(
            database_url=database_url,
            storage_path=Path(
                os.getenv("VALUECHAIN_MONITOR_STORAGE_PATH", "/mnt/hdd8tb")
            ),
            report_dir=Path(
                os.getenv(
                    "VALUECHAIN_MONITOR_REPORT_DIR",
                    str(reports_root / "acquisition-monitor"),
                )
            ),
            sources=_csv_env("VALUECHAIN_MONITOR_SOURCES", DEFAULT_SOURCES),
            services=_csv_env("VALUECHAIN_MONITOR_SERVICES", DEFAULT_SERVICES),
            stale_warning_minutes=int(
                os.getenv("VALUECHAIN_MONITOR_STALE_WARNING_MINUTES", "30")
            ),
            stale_critical_minutes=int(
                os.getenv("VALUECHAIN_MONITOR_STALE_CRITICAL_MINUTES", "90")
            ),
            stuck_claim_minutes=int(
                os.getenv("VALUECHAIN_MONITOR_STUCK_CLAIM_MINUTES", "30")
            ),
            disk_warning_free_percent=float(
                os.getenv("VALUECHAIN_MONITOR_DISK_WARNING_FREE_PERCENT", "5")
            ),
            disk_critical_free_percent=float(
                os.getenv("VALUECHAIN_MONITOR_DISK_CRITICAL_FREE_PERCENT", "2")
            ),
            file_sample_size=int(os.getenv("VALUECHAIN_MONITOR_FILE_SAMPLE_SIZE", "25")),
            webhook_url=os.getenv("VALUECHAIN_MONITOR_WEBHOOK_URL", ""),
            webhook_repeat_hours=int(
                os.getenv("VALUECHAIN_MONITOR_WEBHOOK_REPEAT_HOURS", "6")
            ),
        )


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: Status
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthReport:
    generated_at: str
    overall_status: Status
    checks: tuple[HealthCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "overall_status": self.overall_status,
            "checks": [asdict(check) for check in self.checks],
        }


@dataclass(frozen=True)
class SourceSnapshot:
    source_id: str
    latest_document_at: datetime | None
    documents: int
    document_bytes: int
    source_objects: int
    source_object_bytes: int
    scan_backlog: int
    filing_backlog: int
    source_object_backlog: int
    stale_claims: int
    checkpoint_problems: int
    recent_run_errors: int
    recent_run_items: int
    sampled_paths: tuple[str, ...] = ()

    @property
    def backlog(self) -> int:
        return (
            self.scan_backlog
            + self.filing_backlog
            + self.source_object_backlog
            + self.checkpoint_problems
        )


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def evaluate_source(
    snapshot: SourceSnapshot,
    *,
    now: datetime,
    warning_minutes: int,
    critical_minutes: int,
) -> HealthCheck:
    missing_paths = [path for path in snapshot.sampled_paths if not Path(path).is_file()]
    details = {
        "source_id": snapshot.source_id,
        "documents": snapshot.documents,
        "document_bytes": snapshot.document_bytes,
        "source_objects": snapshot.source_objects,
        "source_object_bytes": snapshot.source_object_bytes,
        "latest_document_at": (
            snapshot.latest_document_at.isoformat() if snapshot.latest_document_at else None
        ),
        "backlog": snapshot.backlog,
        "scan_backlog": snapshot.scan_backlog,
        "filing_backlog": snapshot.filing_backlog,
        "source_object_backlog": snapshot.source_object_backlog,
        "stale_claims": snapshot.stale_claims,
        "checkpoint_problems": snapshot.checkpoint_problems,
        "recent_run_errors": snapshot.recent_run_errors,
        "recent_run_items": snapshot.recent_run_items,
        "sampled_files": len(snapshot.sampled_paths),
        "missing_sampled_files": len(missing_paths),
    }
    if missing_paths:
        details["missing_paths"] = missing_paths[:5]
        return HealthCheck(
            f"source:{snapshot.source_id}",
            "critical",
            "Recent database records point to missing raw files",
            details,
        )
    if snapshot.stale_claims:
        return HealthCheck(
            f"source:{snapshot.source_id}",
            "warning",
            "Issuer claims have remained in running state beyond the limit",
            details,
        )
    if snapshot.backlog == 0:
        return HealthCheck(
            f"source:{snapshot.source_id}",
            "ok",
            "No due acquisition backlog",
            details,
        )
    if snapshot.latest_document_at is None:
        return HealthCheck(
            f"source:{snapshot.source_id}",
            "critical",
            "Acquisition backlog exists but no document has ever completed",
            details,
        )
    age_minutes = (_as_utc(now) - _as_utc(snapshot.latest_document_at)).total_seconds() / 60
    details["latest_document_age_minutes"] = round(age_minutes, 1)
    if age_minutes >= critical_minutes:
        return HealthCheck(
            f"source:{snapshot.source_id}",
            "critical",
            "Acquisition backlog is not making document progress",
            details,
        )
    if age_minutes >= warning_minutes:
        return HealthCheck(
            f"source:{snapshot.source_id}",
            "warning",
            "Acquisition document progress is stale",
            details,
        )
    if snapshot.recent_run_items and (
        snapshot.recent_run_errors / snapshot.recent_run_items >= 0.2
    ):
        return HealthCheck(
            f"source:{snapshot.source_id}",
            "warning",
            "Recent acquisition batches have a high error ratio",
            details,
        )
    return HealthCheck(
        f"source:{snapshot.source_id}",
        "ok",
        "Acquisition is making recent progress",
        details,
    )


def evaluate_disk(
    path: Path,
    *,
    warning_free_percent: float,
    critical_free_percent: float,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> HealthCheck:
    try:
        usage = disk_usage(path)
    except OSError as exc:
        return HealthCheck(
            "storage",
            "critical",
            f"Storage path is unavailable: {type(exc).__name__}: {exc}",
            {"path": str(path)},
        )
    free_percent = 100.0 * usage.free / usage.total if usage.total else 0.0
    details = {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": round(free_percent, 2),
    }
    if free_percent <= critical_free_percent:
        status: Status = "critical"
        message = "Storage free space is below the critical threshold"
    elif free_percent <= warning_free_percent:
        status = "warning"
        message = "Storage free space is below the warning threshold"
    else:
        status = "ok"
        message = "Storage capacity is healthy"
    return HealthCheck("storage", status, message, details)


def parse_systemd_show(output: str) -> dict[str, dict[str, str]]:
    services: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in [*output.splitlines(), ""]:
        if not line.strip():
            service_id = current.get("Id")
            if service_id:
                services[service_id] = current
            current = {}
            continue
        key, separator, value = line.partition("=")
        if separator:
            current[key] = value
    return services


class AcquisitionHealthMonitor:
    def __init__(
        self,
        config: MonitorConfig,
        *,
        now: Callable[[], datetime] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.config = config
        self.now = now or (lambda: datetime.now(UTC))
        self.command_runner = command_runner

    def run(self) -> HealthReport:
        checks = [self._check_database_and_sources(), self._check_services()]
        flattened: list[HealthCheck] = []
        for item in checks:
            flattened.extend(item)
        flattened.append(
            evaluate_disk(
                self.config.storage_path,
                warning_free_percent=self.config.disk_warning_free_percent,
                critical_free_percent=self.config.disk_critical_free_percent,
            )
        )
        overall = max(
            (check.status for check in flattened), key=lambda status: STATUS_RANK[status]
        )
        report = HealthReport(
            generated_at=self.now().astimezone(UTC).isoformat(),
            overall_status=overall,
            checks=tuple(flattened),
        )
        self._persist(report)
        self._maybe_alert(report)
        return report

    def _check_database_and_sources(self) -> list[HealthCheck]:
        try:
            with psycopg.connect(self.config.database_url, row_factory=dict_row) as connection:
                snapshots = self._load_source_snapshots(connection)
        except Exception as exc:  # noqa: BLE001
            return [
                HealthCheck(
                    "database",
                    "critical",
                    f"Postgres health query failed: {type(exc).__name__}: {exc}",
                )
            ]
        checks = [HealthCheck("database", "ok", "Postgres health query succeeded")]
        by_source = {snapshot.source_id: snapshot for snapshot in snapshots}
        for source_id in self.config.sources:
            snapshot = by_source.get(source_id)
            if snapshot is None:
                checks.append(
                    HealthCheck(
                        f"source:{source_id}",
                        "critical",
                        "Expected acquisition source is absent from Postgres",
                    )
                )
                continue
            checks.append(
                evaluate_source(
                    snapshot,
                    now=self.now(),
                    warning_minutes=self.config.stale_warning_minutes,
                    critical_minutes=self.config.stale_critical_minutes,
                )
            )
        return checks

    def _load_source_snapshots(self, connection: psycopg.Connection[Any]) -> list[SourceSnapshot]:
        rows = connection.execute(
            """
            SELECT s.source_id,
              greatest(
                (SELECT max(d.retrieved_at) FROM acquisition_documents d
                  WHERE d.source_id = s.source_id AND d.status = 'complete'),
                (SELECT max(o.retrieved_at) FROM acquisition_source_objects o
                  WHERE o.source_id = s.source_id AND o.status = 'complete')
              ) latest_document_at,
              (SELECT count(*)::int FROM acquisition_documents d
                WHERE d.source_id = s.source_id AND d.status = 'complete') documents,
              (SELECT coalesce(sum(d.byte_size), 0)::bigint FROM acquisition_documents d
                WHERE d.source_id = s.source_id AND d.status = 'complete') document_bytes,
              (SELECT count(*)::int FROM acquisition_source_objects o
                WHERE o.source_id = s.source_id AND o.status = 'complete') source_objects,
              (SELECT coalesce(sum(o.byte_size), 0)::bigint
                FROM acquisition_source_objects o
                WHERE o.source_id = s.source_id AND o.status = 'complete') source_object_bytes,
              (SELECT count(*)::int FROM acquisition_issuer_scans q
                WHERE q.source_id = s.source_id AND (
                  q.status IN ('pending', 'running') OR
                  (q.status = 'retry' AND coalesce(q.next_attempt_at, now()) <= now())
                )) scan_backlog,
              (SELECT count(*)::int FROM acquisition_filings f
                WHERE f.source_id = s.source_id AND (
                  f.status IN ('discovered', 'downloading') OR
                  (f.status = 'retry' AND coalesce(f.next_attempt_at, now()) <= now())
                )) filing_backlog,
              (SELECT count(*)::int FROM acquisition_source_objects o
                WHERE o.source_id = s.source_id AND (
                  o.status IN ('discovered', 'downloading') OR
                  (o.status = 'retry' AND coalesce(o.next_attempt_at, now()) <= now())
                )) source_object_backlog,
              (SELECT count(*)::int FROM acquisition_issuer_scans q
                WHERE q.source_id = s.source_id AND q.status = 'running'
                  AND q.claimed_at < now() - (%s * interval '1 minute')) stale_claims,
              (SELECT count(*)::int FROM acquisition_source_checkpoints c
                WHERE c.source_id = s.source_id AND (
                  (c.status = 'running' AND c.started_at < now() - interval '2 hours') OR
                  (c.status = 'retry' AND coalesce(c.next_attempt_at, now()) <= now())
                )) checkpoint_problems,
              (SELECT coalesce(sum(r.error_count), 0)::int FROM acquisition_runs r
                WHERE r.source_id = s.source_id
                  AND r.started_at > now() - interval '30 minutes') recent_run_errors,
              (SELECT coalesce(sum(r.issuer_count + r.filing_count), 0)::int
                FROM acquisition_runs r WHERE r.source_id = s.source_id
                  AND r.started_at > now() - interval '30 minutes') recent_run_items
            FROM acquisition_sources s
            WHERE s.source_id = ANY(%s)
            ORDER BY s.source_id
            """,
            (self.config.stuck_claim_minutes, list(self.config.sources)),
        ).fetchall()
        path_rows = connection.execute(
            """
            SELECT source_id, local_path FROM (
              SELECT source_id, local_path,
                row_number() OVER (PARTITION BY source_id ORDER BY retrieved_at DESC NULLS LAST) rn
              FROM (
                SELECT source_id, local_path, retrieved_at
                FROM acquisition_documents
                WHERE source_id = ANY(%s) AND status = 'complete'
                UNION ALL
                SELECT source_id, local_path, retrieved_at
                FROM acquisition_source_objects
                WHERE source_id = ANY(%s) AND status = 'complete'
              ) completed_files
            ) ranked WHERE rn <= %s
            """,
            (
                list(self.config.sources),
                list(self.config.sources),
                self.config.file_sample_size,
            ),
        ).fetchall()
        paths: dict[str, list[str]] = {}
        for row in path_rows:
            paths.setdefault(row["source_id"], []).append(row["local_path"])
        return [
            SourceSnapshot(
                **dict(row), sampled_paths=tuple(paths.get(row["source_id"], []))
            )
            for row in rows
        ]

    def _check_services(self) -> list[HealthCheck]:
        if not self.config.services:
            return []
        try:
            result = self.command_runner(
                [
                    "systemctl",
                    "--user",
                    "show",
                    *self.config.services,
                    "--property=Id",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=NRestarts",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return [
                HealthCheck(
                    "services",
                    "critical",
                    f"Could not query systemd services: {type(exc).__name__}: {exc}",
                )
            ]
        parsed = parse_systemd_show(result.stdout)
        checks: list[HealthCheck] = []
        for service in self.config.services:
            fields = parsed.get(service, {})
            active = fields.get("ActiveState") == "active"
            loaded = fields.get("LoadState") == "loaded"
            checks.append(
                HealthCheck(
                    f"service:{service}",
                    "ok" if active and loaded else "critical",
                    "Acquisition service is active"
                    if active and loaded
                    else "Acquisition service is not active",
                    fields,
                )
            )
        return checks

    def _persist(self, report: HealthReport) -> None:
        self.config.report_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True)
        latest = self.config.report_dir / "latest.json"
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.config.report_dir, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, latest)
        history = self.config.report_dir / f"{report.generated_at[:10]}.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")

    def _maybe_alert(self, report: HealthReport) -> None:
        if not self.config.webhook_url or report.overall_status == "ok":
            return
        failing = [
            (check.name, check.status)
            for check in report.checks
            if check.status != "ok"
        ]
        fingerprint = hashlib.sha256(
            json.dumps(failing, sort_keys=True).encode("utf-8")
        ).hexdigest()
        state_path = self.config.report_dir / "alert-state.json"
        previous: dict[str, Any] = {}
        if state_path.exists():
            try:
                previous = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
        last_sent = previous.get("sent_at")
        repeat_due = True
        if last_sent:
            try:
                elapsed = self.now() - datetime.fromisoformat(last_sent)
                repeat_due = elapsed.total_seconds() >= self.config.webhook_repeat_hours * 3600
            except ValueError:
                repeat_due = True
        if previous.get("fingerprint") == fingerprint and not repeat_due:
            return
        try:
            with httpx.Client(timeout=15) as client:
                response = client.post(
                    self.config.webhook_url,
                    json={
                        "text": f"ValueChain acquisition health: {report.overall_status}",
                        "report": report.as_dict(),
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            error_path = self.config.report_dir / "webhook-error.json"
            error_path.write_text(
                json.dumps(
                    {
                        "failed_at": self.now().isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return
        state_path.write_text(
            json.dumps(
                {"fingerprint": fingerprint, "sent_at": self.now().isoformat()},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
