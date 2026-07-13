from __future__ import annotations

import json
from pathlib import Path

from gcu.models import EntityRef, FilingRef

from gcu_priority_markets.monitor import (
    commit_events,
    event_from_filing,
    load_state,
    run_entity_snapshot_monitor,
)


def _filing(identifier: str) -> FilingRef:
    return FilingRef(
        source_id="test_source",
        filing_id=identifier,
        entity_id="entity-1",
        source_entity_id="issuer-1",
        form="annual_report",
        title="Annual Report",
        primary_document_url="https://example.invalid/report.pdf",
        metadata={"jurisdiction": "GB", "issuer_name": "Example plc"},
    )


def test_commit_events_prime_then_deduplicate(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    event = event_from_filing(_filing("f1"), channel="test")
    first = commit_events(
        source_id="test_source",
        observed=[event],
        state_file=state,
        events_file=events,
        prime=True,
    )
    assert first.emitted == 0
    assert not events.exists()
    second = commit_events(
        source_id="test_source",
        observed=[event],
        state_file=state,
        events_file=events,
    )
    assert second.emitted == 0
    assert load_state(state, "test_source").seen_event_ids == [event.event_id]


def test_commit_events_appends_new_event_once(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    event = event_from_filing(_filing("f2"), channel="test")
    report = commit_events(
        source_id="test_source",
        observed=[event],
        state_file=state,
        events_file=events,
    )
    assert report.emitted == 1
    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["filing_id"] == "f2"


def test_entity_snapshot_monitor_detects_add_change_remove(tmp_path: Path) -> None:
    state = tmp_path / "snapshot-state.json"
    events = tmp_path / "snapshot-events.jsonl"
    alpha = EntityRef(
        entity_id="alpha",
        source_id="snapshot_source",
        source_entity_id="A",
        legal_name="Alpha Ltd",
        jurisdiction="SG",
        exchange="XSES",
        ticker="ALP",
    )
    run_entity_snapshot_monitor(
        source_id="snapshot_source",
        entities=[alpha],
        state_file=state,
        events_file=events,
        prime=True,
    )
    changed = alpha.model_copy(update={"legal_name": "Alpha Limited"})
    beta = alpha.model_copy(
        update={"entity_id": "beta", "source_entity_id": "B", "legal_name": "Beta Ltd", "ticker": "BET"}
    )
    report = run_entity_snapshot_monitor(
        source_id="snapshot_source",
        entities=[changed, beta],
        state_file=state,
        events_file=events,
    )
    assert report.emitted == 2
    final = run_entity_snapshot_monitor(
        source_id="snapshot_source",
        entities=[beta],
        state_file=state,
        events_file=events,
    )
    assert final.emitted == 1
