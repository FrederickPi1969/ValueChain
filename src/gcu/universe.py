from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gcu.adapters.sec_edgar import SecEdgarAdapter
from gcu.http import PoliteHttpClient

ENTITY_FIELDS = [
    "entity_id",
    "source_id",
    "source_entity_id",
    "legal_name",
    "jurisdiction",
    "exchange",
    "ticker",
    "lei",
    "isin",
    "local_registry_id",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sec_universe(
    payload: dict[str, Any],
    *,
    raw_output: Path,
    normalized_output: Path,
    source_url: str = SecEdgarAdapter.TICKERS_URL,
    fetched_at: datetime | None = None,
    provenance: str = "SEC official company_tickers_exchange.json",
) -> dict[str, Any]:
    entities = list(SecEdgarAdapter.parse_ticker_payload(payload))
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    normalized_output.parent.mkdir(parents=True, exist_ok=True)
    raw_output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    with normalized_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ENTITY_FIELDS)
        writer.writeheader()
        for entity in entities:
            writer.writerow({field: getattr(entity, field) or "" for field in ENTITY_FIELDS})
    exchanges: dict[str, int] = {}
    for entity in entities:
        key = entity.exchange or "UNSPECIFIED"
        exchanges[key] = exchanges.get(key, 0) + 1
    return {
        "source_id": "sec_edgar",
        "source_url": source_url,
        "provenance": provenance,
        "fetched_at": (fetched_at or datetime.now(UTC)).isoformat(),
        "rows": len(entities),
        "exchange_counts": dict(sorted(exchanges.items())),
        "raw_output": str(raw_output),
        "raw_sha256": sha256_file(raw_output),
        "normalized_output": str(normalized_output),
        "normalized_sha256": sha256_file(normalized_output),
    }


def sync_sec_universe(
    *,
    client: PoliteHttpClient,
    raw_output: Path,
    normalized_output: Path,
) -> dict[str, Any]:
    payload = client.get_json(SecEdgarAdapter.TICKERS_URL)
    if not isinstance(payload, dict) or payload.get("fields") != [
        "cik",
        "name",
        "ticker",
        "exchange",
    ]:
        raise ValueError("SEC ticker payload did not match the expected schema")
    return write_sec_universe(
        payload,
        raw_output=raw_output,
        normalized_output=normalized_output,
    )


def load_sec_snapshot(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "fields" not in payload or "data" not in payload:
        raise ValueError(f"Invalid SEC universe snapshot: {path}")
    return payload


def build_sec_watchlist(
    *,
    universe_csv: Path,
    output_csv: Path,
    tickers: set[str] | None = None,
    ciks: set[str] | None = None,
    exchanges: set[str] | None = None,
) -> dict[str, Any]:
    """Create a database-free SEC monitoring watchlist from the normalized universe."""

    ticker_filter = {value.strip().upper() for value in (tickers or set()) if value.strip()}
    cik_filter = {
        SecEdgarAdapter.normalize_cik(value) for value in (ciks or set()) if value.strip()
    }
    exchange_filter = {value.strip().upper() for value in (exchanges or set()) if value.strip()}
    selected: list[dict[str, str]] = []
    with universe_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            cik = SecEdgarAdapter.normalize_cik(row.get("source_entity_id", ""))
            ticker = row.get("ticker", "").strip().upper()
            exchange = row.get("exchange", "").strip().upper()
            has_filter = bool(ticker_filter or cik_filter or exchange_filter)
            matches = ticker in ticker_filter or cik in cik_filter or exchange in exchange_filter
            if has_filter and not matches:
                continue
            selected.append(
                {
                    "cik": cik,
                    "ticker": row.get("ticker", ""),
                    "company_name": row.get("legal_name", ""),
                    "exchange": row.get("exchange", ""),
                    "source_entity_id": cik,
                }
            )
    selected.sort(key=lambda row: (row["ticker"], row["cik"]))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["cik", "ticker", "company_name", "exchange", "source_entity_id"],
        )
        writer.writeheader()
        writer.writerows(selected)
    return {
        "universe_csv": str(universe_csv),
        "output_csv": str(output_csv),
        "rows": len(selected),
        "ticker_filters": sorted(ticker_filter),
        "cik_filters": sorted(cik_filter),
        "exchange_filters": sorted(exchange_filter),
        "sha256": sha256_file(output_csv),
    }
