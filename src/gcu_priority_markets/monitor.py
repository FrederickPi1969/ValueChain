from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from gcu.models import EntityRef, FilingRef

from gcu_priority_markets.models import DisclosureEvent, MonitorReport, MonitorState


MAX_SEEN_EVENT_IDS = 250_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _stable_hash(parts: Iterable[Any], length: int = 32) -> str:
    text = "|".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def load_state(path: Path, source_id: str) -> MonitorState:
    if not path.exists():
        return MonitorState(source_id=source_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = MonitorState.model_validate(payload)
    if state.source_id != source_id:
        raise ValueError(
            f"State file belongs to {state.source_id!r}, not requested source {source_id!r}"
        )
    return state


def save_state(path: Path, state: MonitorState) -> None:
    state.updated_at = _utcnow()
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(state.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def append_events(path: Path, events: Iterable[DisclosureEvent]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json(exclude_none=True))
            handle.write("\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def event_from_filing(filing: FilingRef, *, channel: str) -> DisclosureEvent:
    metadata = dict(filing.metadata)
    issuer_name = metadata.get("issuer_name") or metadata.get("corp_name") or metadata.get(
        "company_name"
    )
    security_code = metadata.get("security_code") or metadata.get("stock_code") or metadata.get(
        "symbol"
    )
    jurisdiction = metadata.get("jurisdiction") or metadata.get("country") or metadata.get(
        "discovery_country"
    )
    document_urls: list[str] = []
    for candidate in (
        filing.primary_document_url,
        metadata.get("attachment_url"),
        metadata.get("download_url"),
        metadata.get("report_url"),
    ):
        if candidate and str(candidate) not in document_urls:
            document_urls.append(str(candidate))
    published_at: datetime | None = None
    raw_published = metadata.get("published_at") or metadata.get("accepted_at")
    if isinstance(raw_published, datetime):
        published_at = raw_published
    elif raw_published:
        try:
            published_at = datetime.fromisoformat(str(raw_published).replace("Z", "+00:00"))
        except ValueError:
            published_at = None
    event_id = f"{filing.source_id}:{filing.filing_id}"
    return DisclosureEvent(
        event_id=event_id,
        source_id=filing.source_id,
        jurisdiction=str(jurisdiction).upper() if jurisdiction else None,
        channel=channel,
        issuer_id=filing.source_entity_id,
        issuer_name=str(issuer_name) if issuer_name else None,
        security_code=str(security_code) if security_code else None,
        filing_id=filing.filing_id,
        form=filing.form,
        title=filing.title,
        filed_at=filing.filed_at,
        published_at=published_at,
        detail_url=filing.detail_url,
        document_urls=document_urls,
        amendment=filing.amendment,
        metadata=metadata,
    )


def commit_events(
    *,
    source_id: str,
    observed: Iterable[DisclosureEvent],
    state_file: Path,
    events_file: Path,
    prime: bool = False,
    max_seen: int = MAX_SEEN_EVENT_IDS,
) -> MonitorReport:
    started = _utcnow()
    state = load_state(state_file, source_id)
    seen = set(state.seen_event_ids)
    observed_events = list(observed)
    new_events = [event for event in observed_events if event.event_id not in seen]
    emitted_events = [] if prime else new_events
    if emitted_events:
        append_events(events_file, emitted_events)

    # Preserve insertion order while bounding state size. This is deliberately
    # separate from the event log, which remains append-only.
    merged_ids = list(dict.fromkeys([*state.seen_event_ids, *(e.event_id for e in new_events)]))
    if len(merged_ids) > max_seen:
        merged_ids = merged_ids[-max_seen:]
    state.seen_event_ids = merged_ids
    state.cursor["last_observed_at"] = started.isoformat()
    state.cursor["last_observed_count"] = len(observed_events)
    save_state(state_file, state)
    completed = _utcnow()
    return MonitorReport(
        source_id=source_id,
        started_at=started,
        completed_at=completed,
        observed=len(observed_events),
        emitted=len(emitted_events),
        suppressed=len(observed_events) - len(emitted_events),
        primed=prime,
        state_file=str(state_file),
        events_file=str(events_file),
    )


def discover_filings(
    adapter: Any,
    *,
    begin: date,
    end: date,
    entities: Iterable[EntityRef] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> list[FilingRef]:
    options = dict(kwargs or {})
    recent = getattr(adapter, "list_recent_filings", None)
    if callable(recent):
        return list(recent(begin=begin, end=end, **options))
    entity_list = list(entities or [])
    if not entity_list:
        raise ValueError(
            f"{adapter.source_id} requires entities because it has no list_recent_filings method"
        )
    output: list[FilingRef] = []
    for entity in entity_list:
        output.extend(adapter.list_filings(entity, begin=begin, end=end, **options))
    return output


def run_filing_monitor_once(
    adapter: Any,
    *,
    begin: date,
    end: date,
    state_file: Path,
    events_file: Path,
    prime: bool = False,
    entities: Iterable[EntityRef] | None = None,
    kwargs: dict[str, Any] | None = None,
    channel: str | None = None,
) -> MonitorReport:
    filings = discover_filings(adapter, begin=begin, end=end, entities=entities, kwargs=kwargs)
    events = [
        event_from_filing(filing, channel=channel or f"{adapter.source_id}_poll")
        for filing in filings
    ]
    return commit_events(
        source_id=adapter.source_id,
        observed=events,
        state_file=state_file,
        events_file=events_file,
        prime=prime,
    )


def follow_filing_monitor(
    adapter: Any,
    *,
    state_file: Path,
    events_file: Path,
    interval_seconds: float,
    lookback_days: int,
    entities: Iterable[EntityRef] | None = None,
    kwargs: dict[str, Any] | None = None,
    on_report: Callable[[MonitorReport], None] | None = None,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if lookback_days < 1:
        raise ValueError("lookback_days must be at least one")
    while True:
        end = date.today()
        begin = end - timedelta(days=lookback_days)
        report = run_filing_monitor_once(
            adapter,
            begin=begin,
            end=end,
            state_file=state_file,
            events_file=events_file,
            entities=entities,
            kwargs=kwargs,
        )
        if on_report:
            on_report(report)
        time.sleep(interval_seconds)


def entity_snapshot(
    entities: Iterable[EntityRef],
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for entity in entities:
        key = f"{entity.source_id}:{entity.source_entity_id}:{entity.exchange or ''}:{entity.ticker or ''}"
        payload = entity.model_dump(mode="json", exclude_none=True)
        output[key] = {
            "fingerprint": _stable_hash([json.dumps(payload, sort_keys=True, ensure_ascii=False)]),
            "entity": payload,
        }
    return output


def run_entity_snapshot_monitor(
    *,
    source_id: str,
    entities: Iterable[EntityRef],
    state_file: Path,
    events_file: Path,
    prime: bool = False,
) -> MonitorReport:
    started = _utcnow()
    state = load_state(state_file, source_id)
    previous: dict[str, Any] = state.cursor.get("entity_snapshot", {})
    current = entity_snapshot(entities)
    events: list[DisclosureEvent] = []
    keys = sorted(set(previous) | set(current))
    for key in keys:
        before = previous.get(key)
        after = current.get(key)
        if before is None and after is not None:
            action = "listing_added"
            payload = after["entity"]
        elif after is None and before is not None:
            action = "listing_removed"
            payload = before["entity"]
        elif before and after and before.get("fingerprint") != after.get("fingerprint"):
            action = "listing_changed"
            payload = after["entity"]
        else:
            continue
        filing_id = f"{action}:{_stable_hash([key, (after or before).get('fingerprint')])}"
        events.append(
            DisclosureEvent(
                event_id=f"{source_id}:{filing_id}",
                source_id=source_id,
                jurisdiction=payload.get("jurisdiction"),
                channel="entity_snapshot_diff",
                issuer_id=payload.get("source_entity_id"),
                issuer_name=payload.get("legal_name"),
                security_code=payload.get("ticker"),
                filing_id=filing_id,
                form=action,
                title=f"{action}: {payload.get('legal_name') or key}",
                metadata={"action": action, "record_key": key, "entity": payload},
            )
        )
    report = commit_events(
        source_id=source_id,
        observed=events,
        state_file=state_file,
        events_file=events_file,
        prime=prime,
    )
    state = load_state(state_file, source_id)
    state.cursor["entity_snapshot"] = current
    save_state(state_file, state)
    report.started_at = started
    report.completed_at = _utcnow()
    return report
