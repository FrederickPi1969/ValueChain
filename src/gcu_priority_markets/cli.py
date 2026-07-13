from __future__ import annotations

import csv
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from gcu.config import Settings
from gcu.http import PoliteHttpClient
from gcu.models import EntityRef, FilingRef
from gcu.registry import SourceRegistry

from gcu_priority_markets.alerts import read_sedar_alerts
from gcu_priority_markets.capture import capture_official_session
from gcu_priority_markets.catalog import load_contracts, load_priority_markets
from gcu_priority_markets.monitor import (
    commit_events,
    event_from_filing,
    follow_filing_monitor,
    run_entity_snapshot_monitor,
    run_filing_monitor_once,
)
from gcu_priority_markets.registry import PatchRegistry, merge_source_catalog
from gcu_priority_markets.serialization import (
    ENTITY_FIELDS,
    FILING_FIELDS,
    write_json,
    write_jsonl,
    write_models_csv,
)
from gcu_priority_markets.tiering import tier_csv
from valuechain.global_universe_store import (
    GlobalUniverseStore,
    csv_data_row_count,
    deduplicate_entities,
    read_entity_csv,
    read_filing_jsonl,
)


app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Patch-only priority-market company denominators, filing discovery and monitors.",
)

BASE_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "config" / "global_sources_base.yaml"


def _settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


def _date(value: str | None, *, default: date) -> date:
    if value is None:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("Date must use YYYY-MM-DD") from exc


def _emit(payload: Any, output: Path | None = None) -> None:
    if output:
        write_json(output, payload)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _entity_from_row(row: dict[str, str], source_id: str) -> EntityRef:
    source_entity_id = (
        row.get("source_entity_id")
        or row.get("corp_code")
        or row.get("issuer_id")
        or row.get("ticker")
        or row.get("stock_code")
        or row.get("symbol")
        or ""
    ).strip()
    if not source_entity_id:
        raise ValueError(f"Watchlist row has no source identifier: {row}")
    legal_name = (
        row.get("legal_name")
        or row.get("corp_name")
        or row.get("issuer_name")
        or row.get("company_name")
        or source_entity_id
    ).strip()
    ticker = (row.get("ticker") or row.get("stock_code") or row.get("symbol") or "").strip()
    return EntityRef(
        entity_id=(row.get("entity_id") or f"{source_id}-{source_entity_id}").strip(),
        source_id=source_id,
        source_entity_id=source_entity_id,
        legal_name=legal_name,
        jurisdiction=(row.get("jurisdiction") or None),
        exchange=(row.get("exchange") or None),
        ticker=ticker or None,
        lei=(row.get("lei") or None),
        local_registry_id=(row.get("local_registry_id") or source_entity_id),
        metadata=row,
    )


def _read_watchlist(path: Path, source_id: str) -> list[EntityRef]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [_entity_from_row(row, source_id) for row in csv.DictReader(handle)]


def _common_adapter(source_id: str) -> tuple[Settings, PoliteHttpClient, Any]:
    settings = _settings()
    client = PoliteHttpClient(settings)
    registry = PatchRegistry()
    return settings, client, registry.create_adapter(source_id, settings, client)


def _database_url(value: str | None) -> str:
    configured = value or os.getenv("VALUECHAIN_ACQUISITION_DATABASE_URL") or os.getenv(
        "VALUECHAIN_DATABASE_URL"
    )
    if not configured:
        raise typer.BadParameter(
            "Set --database-url or VALUECHAIN_ACQUISITION_DATABASE_URL"
        )
    return configured


def _source_definition(source_id: str):
    """Resolve source metadata from the patch first, then the migrated base catalog."""
    try:
        return PatchRegistry().get(source_id)
    except KeyError:
        return SourceRegistry.load(BASE_SOURCE_CATALOG).get(source_id)


@app.command("doctor")
def doctor(
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    try:
        import gcu

        base_version = getattr(gcu, "__version__", "unknown")
        base_import = True
    except Exception as exc:  # noqa: BLE001
        base_version = f"{type(exc).__name__}: {exc}"
        base_import = False
    registry = PatchRegistry()
    payload = {
        "base_gcu_importable": base_import,
        "base_gcu_version": base_version,
        "patch_version": "0.1.0",
        "patch_source_count": len(registry.all()),
        "priority_market_count": len(load_priority_markets()),
        "python": os.sys.version,
        "cwd": str(Path.cwd()),
    }
    _emit(payload, output)
    if not base_import:
        raise typer.Exit(code=1)


@app.command("markets")
def markets(
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    _emit({"markets": load_priority_markets()}, output)


@app.command("sources")
def sources(
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    contracts = load_contracts()
    payload = {
        "sources": [
            {
                **source.model_dump(mode="json", exclude_none=True),
                "contract": contracts.get(source.source_id, {}),
            }
            for source in PatchRegistry().all()
        ]
    }
    _emit(payload, output)


@app.command("smoke")
def smoke(
    offline: Annotated[bool, typer.Option("--offline/--live")] = True,
    source: Annotated[list[str] | None, typer.Option("--source")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    settings = _settings()
    registry = PatchRegistry()
    selected = source or [item.source_id for item in registry.all()]
    results = []
    with PoliteHttpClient(settings) as client:
        for source_id in selected:
            adapter = registry.create_adapter(source_id, settings, client)
            results.append(adapter.smoke(offline=offline).model_dump(mode="json"))
    failed = [item for item in results if item["status"] == "fail"]
    payload = {
        "offline": offline,
        "checked": len(results),
        "failed": len(failed),
        "results": results,
    }
    _emit(payload, output)
    if failed:
        raise typer.Exit(code=1)


@app.command("apply-overlay")
def apply_overlay(
    base_catalog: Annotated[Path, typer.Option("--base-catalog", exists=True, dir_okay=False)],
    output_catalog: Annotated[Path, typer.Option("--output-catalog")],
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    payload = merge_source_catalog(base_path=base_catalog, output_path=output_catalog)
    _emit(payload, report)


@app.command("universe")
def universe(
    source: Annotated[str, typer.Option("--source")],
    output_csv: Annotated[Path, typer.Option("--output-csv")],
    input_path: Annotated[Path | None, typer.Option("--input-path", exists=True)] = None,
    path: Annotated[list[Path] | None, typer.Option("--path", exists=True)] = None,
    market: Annotated[list[str] | None, typer.Option("--market")] = None,
    jurisdiction: Annotated[list[str] | None, typer.Option("--jurisdiction")] = None,
    auto_download: Annotated[bool, typer.Option("--auto-download")] = False,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    settings, client, adapter = _common_adapter(source)
    try:
        kwargs: dict[str, Any] = {}
        if input_path:
            kwargs["input_path"] = input_path
        if path:
            kwargs["paths"] = path
        if market:
            kwargs["markets"] = market
        if jurisdiction:
            kwargs["jurisdictions"] = jurisdiction
        if auto_download:
            kwargs["auto_download"] = True
        raw_entities = list(adapter.list_entities(**kwargs))
        entities = deduplicate_entities(raw_entities)
        if not entities:
            raise RuntimeError(f"{source} universe returned zero rows")
        count = write_models_csv(output_csv, entities, ENTITY_FIELDS)
        payload = {
            "source_id": source,
            "rows": count,
            "input_rows": len(raw_entities),
            "duplicate_rows_collapsed": len(raw_entities) - count,
            "output_csv": str(output_csv),
            "jurisdiction_counts": {},
            "exchange_counts": {},
        }
        for entity in entities:
            j = entity.jurisdiction or "unknown"
            x = entity.exchange or "unknown"
            payload["jurisdiction_counts"][j] = payload["jurisdiction_counts"].get(j, 0) + 1
            payload["exchange_counts"][x] = payload["exchange_counts"].get(x, 0) + 1
        _emit(payload, report)
    finally:
        client.close()


@app.command("sync-universe")
def sync_universe(
    source: Annotated[str, typer.Option("--source")],
    input_csv: Annotated[Path, typer.Option("--input-csv", exists=True, dir_okay=False)],
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
    source_url: Annotated[str, typer.Option("--source-url")] = "",
    priority: Annotated[int, typer.Option("--priority", min=0)] = 500,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    input_rows = csv_data_row_count(input_csv)
    entities = read_entity_csv(input_csv, source_id=source)
    if not entities:
        raise typer.BadParameter("The normalized universe CSV contains zero valid entity rows")
    definition = _source_definition(source)
    snapshot_source_url = source_url or str(definition.official_url)
    with GlobalUniverseStore(_database_url(database_url)) as store:
        store.upsert_source_definition(definition)
        imported = store.upsert_entities(entities, priority=priority)
        digest = store.record_snapshot(
            source,
            input_csv,
            imported,
            source_url=snapshot_source_url,
            metadata={
                "import_kind": "normalized_entity_csv",
                "input_row_count": input_rows,
                "unique_entity_count": imported,
                "duplicate_rows_collapsed": input_rows - imported,
            },
        )
        counts = store.source_counts()
    _emit(
        {
            "source_id": source,
            "input_csv": str(input_csv),
            "rows_imported": imported,
            "input_rows": input_rows,
            "duplicate_rows_collapsed": input_rows - imported,
            "source_url": snapshot_source_url,
            "sha256": digest,
            "source_counts": counts,
        },
        report,
    )


@app.command("sync-filings")
def sync_filings(
    source: Annotated[str, typer.Option("--source")],
    input_jsonl: Annotated[Path, typer.Option("--input-jsonl", exists=True, dir_okay=False)],
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    filings = read_filing_jsonl(input_jsonl, source_id=source)
    with GlobalUniverseStore(_database_url(database_url)) as store:
        imported = store.upsert_filings(filings)
    _emit(
        {
            "source_id": source,
            "input_jsonl": str(input_jsonl),
            "rows_imported": imported,
        },
        report,
    )


@app.command("database-status")
def database_status(
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    with GlobalUniverseStore(_database_url(database_url)) as store:
        _emit({"source_counts": store.source_counts()}, output)


@app.command("filings")
def filings(
    source: Annotated[str, typer.Option("--source")],
    output_jsonl: Annotated[Path, typer.Option("--output-jsonl")],
    begin: Annotated[str | None, typer.Option("--begin")] = None,
    end: Annotated[str | None, typer.Option("--end")] = None,
    input_path: Annotated[Path | None, typer.Option("--input-path", exists=True)] = None,
    watchlist: Annotated[Path | None, typer.Option("--watchlist", exists=True)] = None,
    market: Annotated[list[str] | None, typer.Option("--market")] = None,
    jurisdiction: Annotated[list[str] | None, typer.Option("--jurisdiction")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages", min=1)] = None,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    end_date = _date(end, default=date.today())
    begin_date = _date(begin, default=end_date - timedelta(days=7))
    settings, client, adapter = _common_adapter(source)
    try:
        options: dict[str, Any] = {}
        if input_path:
            options["input_path"] = input_path
        if market:
            options["markets"] = market
        if jurisdiction:
            options["jurisdictions"] = jurisdiction
        if max_pages:
            options["max_pages"] = max_pages

        if input_path and not hasattr(adapter, "list_recent_filings"):
            rows = list(adapter.list_filings(input_path=input_path))
        elif hasattr(adapter, "list_recent_filings"):
            rows = list(adapter.list_recent_filings(begin=begin_date, end=end_date, **options))
        else:
            entities = _read_watchlist(watchlist, source) if watchlist else []
            if not entities:
                raise typer.BadParameter(
                    "This source requires --watchlist because filings are issuer-scoped"
                )
            rows = []
            for entity in entities:
                rows.extend(adapter.list_filings(entity, begin=begin_date, end=end_date, **options))
        count = write_jsonl(output_jsonl, rows)
        _emit(
            {
                "source_id": source,
                "begin": begin_date.isoformat(),
                "end": end_date.isoformat(),
                "rows": count,
                "output_jsonl": str(output_jsonl),
            },
            report,
        )
    finally:
        client.close()


@app.command("monitor")
def monitor(
    source: Annotated[str, typer.Option("--source")],
    state_file: Annotated[Path, typer.Option("--state-file")],
    events_file: Annotated[Path, typer.Option("--events-file")],
    begin: Annotated[str | None, typer.Option("--begin")] = None,
    end: Annotated[str | None, typer.Option("--end")] = None,
    lookback_days: Annotated[int, typer.Option("--lookback-days", min=1)] = 7,
    input_path: Annotated[Path | None, typer.Option("--input-path", exists=True)] = None,
    watchlist: Annotated[Path | None, typer.Option("--watchlist", exists=True)] = None,
    market: Annotated[list[str] | None, typer.Option("--market")] = None,
    jurisdiction: Annotated[list[str] | None, typer.Option("--jurisdiction")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages", min=1)] = None,
    prime: Annotated[bool, typer.Option("--prime")] = False,
    follow: Annotated[bool, typer.Option("--follow")] = False,
    interval_seconds: Annotated[float, typer.Option("--interval-seconds", min=1)] = 600,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    end_date = _date(end, default=date.today())
    begin_date = _date(begin, default=end_date - timedelta(days=lookback_days))
    settings, client, adapter = _common_adapter(source)
    entities = _read_watchlist(watchlist, source) if watchlist else None
    kwargs: dict[str, Any] = {}
    if input_path:
        kwargs["input_path"] = input_path
    if market:
        kwargs["markets"] = market
    if jurisdiction:
        kwargs["jurisdictions"] = jurisdiction
    if max_pages:
        kwargs["max_pages"] = max_pages
    try:
        if input_path and not hasattr(adapter, "list_recent_filings"):
            filing_rows = list(adapter.list_filings(input_path=input_path))
            observed = [event_from_filing(row, channel="official_export_snapshot") for row in filing_rows]
            result = commit_events(
                source_id=source,
                observed=observed,
                state_file=state_file,
                events_file=events_file,
                prime=prime,
            )
            _emit(result.model_dump(mode="json"), report)
            return
        if follow:
            def show(item: Any) -> None:
                typer.echo(item.model_dump_json())

            follow_filing_monitor(
                adapter,
                state_file=state_file,
                events_file=events_file,
                interval_seconds=interval_seconds,
                lookback_days=lookback_days,
                entities=entities,
                kwargs=kwargs,
                on_report=show,
            )
            return
        result = run_filing_monitor_once(
            adapter,
            begin=begin_date,
            end=end_date,
            state_file=state_file,
            events_file=events_file,
            prime=prime,
            entities=entities,
            kwargs=kwargs,
        )
        _emit(result.model_dump(mode="json"), report)
    finally:
        client.close()


@app.command("snapshot-monitor")
def snapshot_monitor(
    source: Annotated[str, typer.Option("--source")],
    state_file: Annotated[Path, typer.Option("--state-file")],
    events_file: Annotated[Path, typer.Option("--events-file")],
    input_path: Annotated[Path | None, typer.Option("--input-path", exists=True)] = None,
    path: Annotated[list[Path] | None, typer.Option("--path", exists=True)] = None,
    market: Annotated[list[str] | None, typer.Option("--market")] = None,
    jurisdiction: Annotated[list[str] | None, typer.Option("--jurisdiction")] = None,
    prime: Annotated[bool, typer.Option("--prime")] = False,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    settings, client, adapter = _common_adapter(source)
    try:
        kwargs: dict[str, Any] = {}
        if input_path:
            kwargs["input_path"] = input_path
        if path:
            kwargs["paths"] = path
        if market:
            kwargs["markets"] = market
        if jurisdiction:
            kwargs["jurisdictions"] = jurisdiction
        entities = list(adapter.list_entities(**kwargs))
        result = run_entity_snapshot_monitor(
            source_id=source,
            entities=entities,
            state_file=state_file,
            events_file=events_file,
            prime=prime,
        )
        _emit(result.model_dump(mode="json"), report)
    finally:
        client.close()


@app.command("opendart-monitor")
def opendart_monitor(
    base_catalog: Annotated[Path, typer.Option("--base-catalog", exists=True, dir_okay=False)],
    watchlist: Annotated[Path, typer.Option("--watchlist", exists=True, dir_okay=False)],
    state_file: Annotated[Path, typer.Option("--state-file")],
    events_file: Annotated[Path, typer.Option("--events-file")],
    begin: Annotated[str | None, typer.Option("--begin")] = None,
    end: Annotated[str | None, typer.Option("--end")] = None,
    lookback_days: Annotated[int, typer.Option("--lookback-days", min=1)] = 7,
    prime: Annotated[bool, typer.Option("--prime")] = False,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    end_date = _date(end, default=date.today())
    begin_date = _date(begin, default=end_date - timedelta(days=lookback_days))
    settings = _settings()
    entities = _read_watchlist(watchlist, "opendart")
    with PoliteHttpClient(settings) as client:
        registry = SourceRegistry.load(base_catalog)
        adapter = registry.create_adapter("opendart", settings, client)
        result = run_filing_monitor_once(
            adapter,
            begin=begin_date,
            end=end_date,
            state_file=state_file,
            events_file=events_file,
            prime=prime,
            entities=entities,
            channel="opendart_watchlist",
        )
    _emit(result.model_dump(mode="json"), report)


@app.command("sedar-alerts")
def sedar_alerts(
    mail_path: Annotated[Path, typer.Option("--mail-path", exists=True)],
    state_file: Annotated[Path, typer.Option("--state-file")],
    events_file: Annotated[Path, typer.Option("--events-file")],
    prime: Annotated[bool, typer.Option("--prime")] = False,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    events = read_sedar_alerts(mail_path)
    result = commit_events(
        source_id="sedar_plus",
        observed=events,
        state_file=state_file,
        events_file=events_file,
        prime=prime,
    )
    _emit(result.model_dump(mode="json"), report)


@app.command("firds-files")
def firds_files(
    begin: Annotated[str | None, typer.Option("--begin")] = None,
    end: Annotated[str | None, typer.Option("--end")] = None,
    file_type: Annotated[str, typer.Option("--file-type")] = "FULINS",
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    end_date = _date(end, default=date.today())
    begin_date = _date(begin, default=end_date - timedelta(days=21))
    settings, client, adapter = _common_adapter("fca_firds_priority")
    try:
        refs = adapter.list_files(begin=begin_date, end=end_date, file_type=file_type)
        _emit(
            {
                "file_type": file_type,
                "begin": begin_date.isoformat(),
                "end": end_date.isoformat(),
                "files": [item.model_dump(mode="json") for item in refs],
            },
            output,
        )
    finally:
        client.close()


@app.command("firds-download")
def firds_download(
    output_dir: Annotated[Path, typer.Option("--output-dir")],
    as_of: Annotated[str | None, typer.Option("--as-of")] = None,
    lookback_days: Annotated[int, typer.Option("--lookback-days", min=1)] = 21,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    settings, client, adapter = _common_adapter("fca_firds_priority")
    try:
        refs = adapter.latest_equity_full_files(
            lookback_days=lookback_days,
            as_of=_date(as_of, default=date.today()),
        )
        paths = adapter.download_files(refs, output_dir)
        _emit(
            {
                "files": [item.model_dump(mode="json") for item in refs],
                "paths": [str(item) for item in paths],
            },
            report,
        )
    finally:
        client.close()


@app.command("firds-delta-monitor")
def firds_delta_monitor(
    path: Annotated[list[Path], typer.Option("--path", exists=True)],
    state_file: Annotated[Path, typer.Option("--state-file")],
    events_file: Annotated[Path, typer.Option("--events-file")],
    jurisdiction: Annotated[list[str] | None, typer.Option("--jurisdiction")] = None,
    prime: Annotated[bool, typer.Option("--prime")] = False,
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    settings, client, adapter = _common_adapter("fca_firds_priority")
    try:
        observed = list(
            adapter.list_delta_events(paths=path, jurisdictions=jurisdiction)
        )
        result = commit_events(
            source_id="fca_firds_priority",
            observed=observed,
            state_file=state_file,
            events_file=events_file,
            prime=prime,
        )
        _emit(result.model_dump(mode="json"), report)
    finally:
        client.close()


@app.command("tier")
def tier(
    input_csv: Annotated[Path, typer.Option("--input-csv", exists=True, dir_okay=False)],
    output_csv: Annotated[Path, typer.Option("--output-csv")],
    report: Annotated[Path | None, typer.Option("--report")] = None,
) -> None:
    _emit(tier_csv(input_csv, output_csv), report)


@app.command("capture-official")
def capture_official(
    source: Annotated[str, typer.Option("--source")],
    output_dir: Annotated[Path, typer.Option("--output-dir")],
    headed: Annotated[bool, typer.Option("--headed")] = False,
    wait_seconds: Annotated[float, typer.Option("--wait-seconds", min=1)] = 15.0,
    page_url: Annotated[str | None, typer.Option("--page-url")] = None,
    pattern: Annotated[list[str] | None, typer.Option("--pattern")] = None,
    click_text: Annotated[list[str] | None, typer.Option("--click-text")] = None,
    click_selector: Annotated[list[str] | None, typer.Option("--click-selector")] = None,
) -> None:
    payload = capture_official_session(
        source_id=source,
        output_dir=output_dir,
        headless=not headed,
        wait_seconds=wait_seconds,
        page_url=page_url,
        patterns=pattern,
        click_texts=click_text,
        click_selectors=click_selector,
    )
    _emit(payload)


if __name__ == "__main__":
    app()
