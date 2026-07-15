from __future__ import annotations

import asyncio
import csv
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from valuechain.filing_parser import parse_sections, segment_passages
from valuechain.financial_ie.ixbrl import IXBRL_EXTRACTOR_VERSION, extract_financial_facts
from valuechain.financial_ie.json_utils import parse_json_payload, recover_partial_object_array
from valuechain.financial_ie.llm import AsyncLLMClient, AsyncLLMConfig
from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.pilot_prompts import (
    PROFILE_SYSTEM,
    SIGNALS_SYSTEM,
    build_profile_prompt,
    build_signals_prompt,
    normalize_profile,
    normalize_signals,
)
from valuechain.financial_ie.pilot_sources import CatalogConfig, build_filing_manifest, load_pilot_universe
from valuechain.financial_ie.quality import audit_records, write_quality_issues
from valuechain.financial_ie.retrieval import BM25Index, include_anchor_chunks
from valuechain.models import FilingRecord
from valuechain.relevance import score_passage


PILOT_VERSION = "financial-ie-pilot-v0.1"

SIGNAL_QUERIES: tuple[str, ...] = (
    "demand revenue growth decline bookings backlog customer spending",
    "pricing gross margin operating margin costs inflation",
    "capital expenditures investment repurchase dividend debt allocation",
    "capacity supply constraint shortage manufacturing production inventory",
    "major customer customer concentration accounted for revenue",
    "rely depend supplier cloud data center power hosting third party",
    "regulation export controls tariffs sanctions geopolitical government",
    "new product technology transition research development launch",
    "strategic partnership acquisition merger joint venture collaboration",
    "liquidity cash flow debt covenant credit facility going concern",
)


@dataclass(frozen=True, slots=True)
class PilotRunConfig:
    output_dir: Path
    universe_path: Path = Path("data/universe/ai_infra_universe.csv")
    target_count: int = 100
    model: str = "Qwen/Qwen3.6-35B-A3B"
    llm_base_url: str = "http://100.114.26.88:31969/v1"
    llm_api_key: str = "1969"
    concurrency: int = 4
    catalog: CatalogConfig = CatalogConfig()


class FinancialIEPilot:
    def __init__(self, config: PilotRunConfig) -> None:
        self.config = config
        self._parse_semaphore = asyncio.Semaphore(max(1, config.concurrency))
        self._write_lock = asyncio.Lock()

    async def run(self) -> dict[str, Any]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.config.output_dir / "filing_manifest.jsonl"
        companies = load_pilot_universe(
            self.config.universe_path,
            target_count=self.config.target_count,
        )
        if manifest_path.exists():
            existing_manifest = {
                str(row.get("ticker")): row for row in read_jsonl(manifest_path)
            }
            manifest = [
                existing_manifest.get(
                    str(company.get("ticker")),
                    {**company, "status": "missing_manifest_entry", "error": "Manifest entry not built"},
                )
                for company in companies
            ]
            retry_companies = [row for row in manifest if row.get("status") != "ready"]
            if retry_companies:
                refreshed = await build_filing_manifest(retry_companies, self.config.catalog)
                refreshed_by_ticker = {str(row.get("ticker")): row for row in refreshed}
                manifest = [refreshed_by_ticker.get(str(row.get("ticker")), row) for row in manifest]
                write_jsonl(manifest_path, manifest)
        else:
            manifest = await build_filing_manifest(companies, self.config.catalog)
            write_jsonl(manifest_path, manifest)
        records_path = self.config.output_dir / "company_records.jsonl"
        completed = {
            str(row.get("ticker"))
            for row in read_jsonl(records_path)
            if row.get("status") == "complete"
        }
        pending = [row for row in manifest if str(row.get("ticker")) not in completed]
        llm_config = AsyncLLMConfig(
            base_url=self.config.llm_base_url,
            api_key=self.config.llm_api_key,
            model=self.config.model,
            concurrency=self.config.concurrency,
        )
        async with AsyncLLMClient(llm_config) as client:
            await asyncio.gather(*(self._execute(row, client, records_path) for row in pending))
        records_by_ticker: dict[str, dict[str, Any]] = {}
        target_tickers = {str(row.get("ticker") or "") for row in manifest}
        for row in read_jsonl(records_path):
            ticker = str(row.get("ticker") or "")
            if ticker in target_tickers:
                records_by_ticker[ticker] = row
        records = sorted(records_by_ticker.values(), key=lambda row: str(row.get("ticker") or ""))
        await asyncio.gather(*(self._refresh_financial_facts(row) for row in records))
        annotate_evidence_failure_reasons(records)
        write_jsonl(records_path, records)
        summary = materialize_audit_outputs(self.config.output_dir, manifest, records, self.config)
        return summary

    async def _refresh_financial_facts(self, row: dict[str, Any]) -> None:
        if row.get("status") != "complete" or not row.get("local_path"):
            return
        versions = {fact.get("extractor_version") for fact in row.get("financial_facts", [])}
        if versions == {IXBRL_EXTRACTOR_VERSION}:
            return
        async with self._parse_semaphore:
            row["financial_facts"] = await asyncio.to_thread(extract_facts_with_provenance, row)

    async def _execute(
        self,
        manifest_row: dict[str, Any],
        client: AsyncLLMClient,
        records_path: Path,
    ) -> None:
        try:
            row = await self._process_company(manifest_row, client)
        except Exception as exc:
            row = {
                **manifest_row,
                "status": "extraction_error",
                "error": f"{type(exc).__name__}: {exc}",
                "pilot_version": PILOT_VERSION,
                "extractor_model": self.config.model,
                "profile": {},
                "financial_facts": [],
                "material_signals": [],
                "evidence_chunks": [],
            }
        async with self._write_lock:
            append_jsonl(records_path, row)

    async def _process_company(
        self,
        manifest_row: dict[str, Any],
        client: AsyncLLMClient,
    ) -> dict[str, Any]:
        if manifest_row.get("status") != "ready":
            return {
                **manifest_row,
                "pilot_version": PILOT_VERSION,
                "extractor_model": self.config.model,
                "profile": {},
                "financial_facts": [],
                "material_signals": [],
                "evidence_chunks": [],
            }
        async with self._parse_semaphore:
            parsed = await asyncio.to_thread(parse_filing, manifest_row)
        chunks: list[DocumentChunk] = parsed["chunks"]
        chunk_map = {chunk.chunk_id: chunk for chunk in chunks}
        profile_chunks = select_profile_chunks(chunks)
        signal_chunks = select_signal_chunks(chunks, parsed["passage_relevance"])
        profile_response = await client.complete(
            PROFILE_SYSTEM,
            build_profile_prompt(str(manifest_row["company_name"]), profile_chunks),
            max_tokens=1600,
        )
        profile_parse_error = ""
        try:
            profile_payload = parse_json_payload(profile_response.content)
        except ValueError as exc:
            profile_payload = {}
            profile_parse_error = str(exc)
        profile = normalize_profile(profile_payload, chunk_map)
        signal_response = await client.complete(
            SIGNALS_SYSTEM,
            build_signals_prompt(str(manifest_row["company_name"]), signal_chunks, profile),
            max_tokens=3600,
        )
        signal_parse_error = ""
        try:
            signal_payload = parse_json_payload(signal_response.content)
        except ValueError as exc:
            signal_payload = {}
            signal_parse_error = str(exc)
        if not valid_signal_payload(signal_payload):
            recovered_signals = recover_partial_object_array(signal_response.content, "signals")
            if recovered_signals:
                signal_payload = {"signals": recovered_signals}
                signal_parse_error = f"partial_array_recovery:{len(recovered_signals)}"
        signals = normalize_signals(signal_payload, chunk_map)
        enrich_evidence_provenance(profile, signals, manifest_row)
        cited_ids = {
            item.get("chunk_id")
            for item in profile.get("evidence", [])
            if item.get("chunk_id")
        } | {signal.get("chunk_id") for signal in signals if signal.get("chunk_id")}
        evidence_chunks = [
            {
                "chunk_id": chunk.chunk_id,
                "section": chunk.section_hint,
                "text": chunk.text,
                "accession_number": manifest_row.get("accession_number"),
                "source_document_url": manifest_row.get("source_document_url"),
            }
            for chunk in chunks
            if chunk.chunk_id in cited_ids
        ]
        return {
            **manifest_row,
            "status": "complete",
            "error": "",
            "pilot_version": PILOT_VERSION,
            "extractor_model": self.config.model,
            "profile": profile,
            "financial_facts": parsed["financial_facts"],
            "material_signals": signals,
            "evidence_chunks": evidence_chunks,
            "raw_model_outputs": {
                "profile": profile_response.content,
                "signals": signal_response.content,
            },
            "diagnostics": {
                "section_count": parsed["section_count"],
                "chunk_count": len(chunks),
                "profile_chunk_count": len(profile_chunks),
                "signal_chunk_count": len(signal_chunks),
                "parser_warnings": parsed["warnings"],
                "profile_latency_s": profile_response.latency_s,
                "signal_latency_s": signal_response.latency_s,
                "profile_attempts": profile_response.attempts,
                "signal_attempts": signal_response.attempts,
                "profile_output_chars": len(profile_response.content),
                "signal_output_chars": len(signal_response.content),
                "profile_payload_type": type(profile_payload).__name__,
                "signal_payload_type": type(signal_payload).__name__,
                "profile_parse_error": profile_parse_error,
                "signal_parse_error": signal_parse_error,
            },
        }


def parse_filing(manifest_row: dict[str, Any]) -> dict[str, Any]:
    filing = FilingRecord(
        ticker=str(manifest_row["ticker"]),
        cik=str(manifest_row.get("cik") or ""),
        company_name=str(manifest_row["company_name"]),
        form=str(manifest_row["form"]),
        accession_number=str(manifest_row["accession_number"]),
        filing_date=str(manifest_row["filing_date"]),
        report_date=str(manifest_row.get("report_date") or ""),
        accepted_timestamp=str(manifest_row.get("accepted_timestamp") or ""),
        archive_url=str(manifest_row.get("archive_url") or ""),
        primary_document_url=str(manifest_row.get("source_document_url") or ""),
        local_path=str(manifest_row["local_path"]),
    )
    sections = parse_sections(filing)
    passages = [passage for section in sections for passage in segment_passages(section)]
    relevance = {passage.passage_id: score_passage(passage).relevance_score for passage in passages}
    chunks = [
        DocumentChunk(
            chunk_id=passage.passage_id,
            text=passage.text,
            section_hint=passage.section,
        )
        for passage in passages
    ]
    facts = extract_facts_with_provenance(manifest_row)
    return {
        "chunks": chunks,
        "passage_relevance": relevance,
        "financial_facts": facts,
        "section_count": len(sections),
        "warnings": sorted({warning for section in sections for warning in section.warnings}),
    }


def extract_facts_with_provenance(row: dict[str, Any]) -> list[dict[str, Any]]:
    facts = extract_financial_facts(
        Path(str(row["local_path"])),
        report_date=str(row.get("report_date") or ""),
    )
    for fact in facts:
        fact.update(
            {
                "accession_number": row.get("accession_number"),
                "filing_form": row.get("form"),
                "filing_date": row.get("filing_date"),
                "source_document_url": row.get("source_document_url"),
            }
        )
    return facts


def select_profile_chunks(chunks: list[DocumentChunk], *, limit: int = 12) -> list[DocumentChunk]:
    if not chunks:
        return []
    business = [
        chunk
        for chunk in chunks
        if any(token in chunk.section_hint for token in ("business", "company_information", "full_filing"))
    ]
    anchors = business[:3] if business else chunks[:2]
    ranked = BM25Index(chunks).search(
        "principal products services solutions customers markets business segments operations competition",
        limit=30,
        expand=False,
    )
    return include_anchor_chunks(ranked, anchors, limit=limit)


def select_signal_chunks(
    chunks: list[DocumentChunk],
    relevance: dict[str, float],
    *,
    limit: int = 18,
) -> list[DocumentChunk]:
    if not chunks:
        return []
    index = BM25Index(chunks)
    reciprocal: Counter[str] = Counter()
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    anchors: list[DocumentChunk] = []
    for query in SIGNAL_QUERIES:
        matches = index.search(query, limit=6, expand=True)
        anchors.extend(matches[:1])
        for rank, chunk in enumerate(matches, start=1):
            reciprocal[chunk.chunk_id] += 1 / (20 + rank)
    for chunk_id, score in sorted(relevance.items(), key=lambda item: item[1], reverse=True)[:12]:
        if score > 0:
            reciprocal[chunk_id] += score / 100
    ranked = [
        chunks_by_id[chunk_id]
        for chunk_id, _ in reciprocal.most_common()
        if chunk_id in chunks_by_id
    ]
    return include_anchor_chunks(ranked, anchors, limit=limit)


def enrich_evidence_provenance(
    profile: dict[str, Any],
    signals: list[dict[str, Any]],
    manifest_row: dict[str, Any],
) -> None:
    provenance = {
        "accession_number": manifest_row.get("accession_number"),
        "filing_form": manifest_row.get("form"),
        "filing_date": manifest_row.get("filing_date"),
        "source_document_url": manifest_row.get("source_document_url"),
    }
    for evidence in profile.get("evidence", []):
        evidence.update(provenance)
    for signal in signals:
        signal.update(provenance)


def annotate_evidence_failure_reasons(records: list[dict[str, Any]]) -> None:
    for record in records:
        chunks = {
            str(chunk.get("chunk_id") or ""): str(chunk.get("text") or "")
            for chunk in record.get("evidence_chunks", [])
        }
        for signal in record.get("material_signals", []):
            signal["evidence_failure_reason"] = evidence_failure_reason(
                signal.get("evidence_valid"),
                str(signal.get("chunk_id") or ""),
                str(signal.get("evidence_quote") or ""),
                chunks,
            )
        for evidence in record.get("profile", {}).get("evidence", []):
            evidence["evidence_failure_reason"] = evidence_failure_reason(
                evidence.get("evidence_valid"),
                str(evidence.get("chunk_id") or ""),
                str(evidence.get("quote") or ""),
                chunks,
            )


def evidence_failure_reason(
    evidence_valid: Any,
    chunk_id: str,
    quote: str,
    chunks: dict[str, str],
) -> str:
    if evidence_valid is True:
        return ""
    if not chunk_id:
        return "missing_chunk_id"
    if chunk_id not in chunks:
        return "unknown_chunk_id"
    if not quote:
        return "missing_quote"
    return "quote_not_found"


def valid_signal_payload(payload: Any) -> bool:
    return isinstance(payload, list) or (
        isinstance(payload, dict) and isinstance(payload.get("signals"), list)
    )


def materialize_audit_outputs(
    output_dir: Path,
    manifest: list[dict[str, Any]],
    records: list[dict[str, Any]],
    config: PilotRunConfig,
) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for record in records:
        identity = {
            "ticker": record.get("ticker"),
            "company_name": record.get("company_name"),
            "cik": record.get("cik"),
        }
        facts.extend({**identity, **fact} for fact in record.get("financial_facts", []))
        signals.extend({**identity, **signal} for signal in record.get("material_signals", []))
    write_jsonl(output_dir / "financial_facts.jsonl", facts)
    write_jsonl(output_dir / "material_signals.jsonl", signals)
    write_coverage_csv(output_dir / "coverage.csv", manifest, records)
    write_review_sample(output_dir / "review_sample.csv", signals)
    write_profile_review_sample(output_dir / "profile_review_sample.csv", records)
    quality_issues, quality_summary = audit_records(records)
    write_quality_issues(output_dir / "quality_issues.csv", quality_issues)
    complete = [row for row in records if row.get("status") == "complete"]
    signal_evidence_valid = sum(signal.get("evidence_valid") is True for signal in signals)
    profile_evidence_total = sum(len(row.get("profile", {}).get("evidence", [])) for row in complete)
    profile_evidence_valid = sum(
        item.get("evidence_valid") is True
        for row in complete
        for item in row.get("profile", {}).get("evidence", [])
    )
    evidence_total = len(signals) + profile_evidence_total
    evidence_valid = signal_evidence_valid + profile_evidence_valid
    fact_company_coverage = {
        str(field): len(
            {
                str(fact.get("ticker"))
                for fact in facts
                if fact.get("field") == field and fact.get("ticker")
            }
        )
        for field in {fact.get("field") for fact in facts if fact.get("field")}
    }
    signal_counts = Counter(signal.get("category") for signal in signals)
    signal_failure_counts = Counter(
        str(signal.get("evidence_failure_reason"))
        for signal in signals
        if signal.get("evidence_failure_reason")
    )
    profile_failure_counts = Counter(
        str(item.get("evidence_failure_reason"))
        for row in complete
        for item in row.get("profile", {}).get("evidence", [])
        if item.get("evidence_failure_reason")
    )
    summary = {
        "pilot_version": PILOT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "model": config.model,
        "requested_companies": len(manifest),
        "catalog_ready": sum(row.get("status") == "ready" for row in manifest),
        "completed_companies": len(complete),
        "failed_or_missing_companies": len(manifest) - len(complete),
        "profile_industry_coverage": round(
            sum(bool(row.get("profile", {}).get("primary_industry")) for row in complete) / max(1, len(complete)),
            4,
        ),
        "financial_fact_count": len(facts),
        "financial_fact_company_coverage": dict(sorted(fact_company_coverage.items())),
        "material_signal_count": len(signals),
        "material_signal_categories": dict(sorted(signal_counts.items())),
        "signal_evidence_valid_count": signal_evidence_valid,
        "signal_evidence_total_count": len(signals),
        "signal_evidence_valid_rate": round(signal_evidence_valid / max(1, len(signals)), 4),
        "signal_evidence_failure_reasons": dict(sorted(signal_failure_counts.items())),
        "profile_evidence_valid_count": profile_evidence_valid,
        "profile_evidence_total_count": profile_evidence_total,
        "profile_evidence_valid_rate": round(profile_evidence_valid / max(1, profile_evidence_total), 4),
        "profile_evidence_failure_reasons": dict(sorted(profile_failure_counts.items())),
        "evidence_valid_count": evidence_valid,
        "evidence_total_count": evidence_total,
        "evidence_valid_rate": round(evidence_valid / max(1, evidence_total), 4),
        "companies_with_no_signals": sorted(
            str(row.get("ticker")) for row in complete if not row.get("material_signals")
        ),
        "companies_with_fewer_than_five_signals": sorted(
            str(row.get("ticker")) for row in complete if len(row.get("material_signals", [])) < 5
        ),
        "companies_with_parser_warnings": sorted(
            str(row.get("ticker"))
            for row in complete
            if row.get("diagnostics", {}).get("parser_warnings")
        ),
        "companies_with_partial_signal_recovery": sorted(
            str(row.get("ticker"))
            for row in complete
            if str(row.get("diagnostics", {}).get("signal_parse_error") or "").startswith(
                "partial_array_recovery:"
            )
        ),
        "database_writes": 0,
        **quality_summary,
        "artifacts": {
            "manifest": "filing_manifest.jsonl",
            "company_records": "company_records.jsonl",
            "financial_facts": "financial_facts.jsonl",
            "material_signals": "material_signals.jsonl",
            "coverage": "coverage.csv",
            "review_sample": "review_sample.csv",
            "profile_review_sample": "profile_review_sample.csv",
            "quality_issues": "quality_issues.csv",
        },
    }
    (output_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def write_coverage_csv(
    path: Path,
    manifest: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> None:
    records_by_ticker = {str(row.get("ticker")): row for row in records}
    fields = [
        "ticker",
        "company_name",
        "status",
        "form",
        "filing_date",
        "accession_number",
        "primary_industry",
        "financial_fact_count",
        "material_signal_count",
        "valid_signal_evidence_rate",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for source in manifest:
            record = records_by_ticker.get(str(source.get("ticker")), source)
            signals = record.get("material_signals", [])
            writer.writerow(
                {
                    "ticker": source.get("ticker"),
                    "company_name": source.get("company_name"),
                    "status": record.get("status"),
                    "form": source.get("form"),
                    "filing_date": source.get("filing_date"),
                    "accession_number": source.get("accession_number"),
                    "primary_industry": record.get("profile", {}).get("primary_industry"),
                    "financial_fact_count": len(record.get("financial_facts", [])),
                    "material_signal_count": len(signals),
                    "valid_signal_evidence_rate": round(
                        sum(signal.get("evidence_valid") is True for signal in signals) / max(1, len(signals)),
                        4,
                    ),
                    "error": record.get("error"),
                }
            )


def write_review_sample(path: Path, signals: list[dict[str, Any]], *, limit: int = 100) -> None:
    def sort_key(row: dict[str, Any]) -> tuple[int, str]:
        return -int(row.get("significance") or 1), str(row.get("ticker") or "")

    half = max(1, limit // 2)
    invalid = sorted((row for row in signals if row.get("evidence_valid") is not True), key=sort_key)[:half]
    valid = sorted((row for row in signals if row.get("evidence_valid") is True), key=sort_key)[: limit - len(invalid)]
    selected = [
        {**row, "review_bucket": "evidence_failed" if row.get("evidence_valid") is not True else "evidence_valid"}
        for row in [*invalid, *valid]
    ]
    fields = [
        "ticker",
        "company_name",
        "category",
        "headline",
        "modality",
        "significance",
        "statement",
        "evidence_quote",
        "evidence_valid",
        "evidence_failure_reason",
        "source_section",
        "accession_number",
        "source_document_url",
        "review_bucket",
        "human_label",
        "review_notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in selected:
            writer.writerow(row)


def write_profile_review_sample(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "ticker",
        "company_name",
        "primary_industry",
        "strategic_domains",
        "value_chain_roles",
        "business_summary",
        "products_services",
        "end_markets",
        "strategic_importance",
        "strategic_importance_rationale",
        "evidence_valid",
        "evidence_failure_reasons",
        "evidence_quotes",
        "accession_number",
        "source_document_url",
        "human_label",
        "review_notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            if record.get("status") != "complete":
                continue
            profile = record.get("profile", {})
            writer.writerow(
                {
                    "ticker": record.get("ticker"),
                    "company_name": record.get("company_name"),
                    "primary_industry": profile.get("primary_industry"),
                    "strategic_domains": " | ".join(profile.get("strategic_domains", [])),
                    "value_chain_roles": " | ".join(profile.get("value_chain_roles", [])),
                    "business_summary": profile.get("business_summary"),
                    "products_services": " | ".join(profile.get("products_services", [])),
                    "end_markets": " | ".join(profile.get("end_markets", [])),
                    "strategic_importance": profile.get("strategic_importance"),
                    "strategic_importance_rationale": profile.get("strategic_importance_rationale"),
                    "evidence_valid": profile.get("evidence_valid"),
                    "evidence_failure_reasons": " | ".join(
                        str(item.get("evidence_failure_reason") or "")
                        for item in profile.get("evidence", [])
                        if item.get("evidence_failure_reason")
                    ),
                    "evidence_quotes": " | ".join(
                        str(item.get("quote") or "") for item in profile.get("evidence", [])
                    ),
                    "accession_number": record.get("accession_number"),
                    "source_document_url": record.get("source_document_url"),
                    "human_label": "",
                    "review_notes": "",
                }
            )


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
