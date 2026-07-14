from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from valuechain.acquisition_monitor import (
    SourceSnapshot,
    evaluate_disk,
    evaluate_source,
    parse_systemd_show,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def snapshot(**overrides):
    values = {
        "source_id": "sec_edgar",
        "latest_document_at": NOW - timedelta(minutes=2),
        "documents": 100,
        "document_bytes": 1_000,
        "scan_backlog": 10,
        "filing_backlog": 0,
        "stale_claims": 0,
        "checkpoint_problems": 0,
        "recent_run_errors": 0,
        "recent_run_items": 20,
        "sampled_paths": (),
    }
    values.update(overrides)
    return SourceSnapshot(**values)


def evaluate(value: SourceSnapshot):
    return evaluate_source(
        value,
        now=NOW,
        warning_minutes=30,
        critical_minutes=90,
    )


def test_source_with_recent_progress_is_healthy() -> None:
    result = evaluate(snapshot())
    assert result.status == "ok"
    assert result.details["backlog"] == 10


def test_idle_source_does_not_trigger_staleness() -> None:
    result = evaluate(
        snapshot(
            latest_document_at=NOW - timedelta(days=10),
            scan_backlog=0,
        )
    )
    assert result.status == "ok"
    assert result.message == "No due acquisition backlog"


def test_stale_progress_escalates_by_age() -> None:
    warning = evaluate(snapshot(latest_document_at=NOW - timedelta(minutes=31)))
    critical = evaluate(snapshot(latest_document_at=NOW - timedelta(minutes=91)))
    assert warning.status == "warning"
    assert critical.status == "critical"


def test_missing_recent_file_is_critical(tmp_path: Path) -> None:
    present = tmp_path / "present.pdf"
    present.write_bytes(b"pdf")
    result = evaluate(
        snapshot(sampled_paths=(str(present), str(tmp_path / "missing.pdf")))
    )
    assert result.status == "critical"
    assert result.details["missing_sampled_files"] == 1


def test_stuck_claim_is_warning() -> None:
    result = evaluate(snapshot(stale_claims=2))
    assert result.status == "warning"


def test_disk_thresholds() -> None:
    def usage(_path: Path) -> shutil._ntuple_diskusage:
        return shutil._ntuple_diskusage(total=1000, used=960, free=40)

    result = evaluate_disk(
        Path("/data"),
        warning_free_percent=5,
        critical_free_percent=2,
        disk_usage=usage,
    )
    assert result.status == "warning"
    assert result.details["free_percent"] == 4.0


def test_parse_systemd_show_separates_units() -> None:
    rows = parse_systemd_show(
        """Id=a.service
LoadState=loaded
ActiveState=active

Id=b.service
LoadState=loaded
ActiveState=inactive
"""
    )
    assert rows["a.service"]["ActiveState"] == "active"
    assert rows["b.service"]["ActiveState"] == "inactive"
