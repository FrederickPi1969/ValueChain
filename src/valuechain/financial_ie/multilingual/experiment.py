from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from valuechain.financial_ie.json_utils import parse_json_payload, recover_partial_object_array
from valuechain.financial_ie.llm import AsyncLLMClient, AsyncLLMConfig
from valuechain.financial_ie.multilingual.citation_repair import (
    apply_repairs,
    collect_repair_items,
)
from valuechain.financial_ie.multilingual.documents import (
    load_source_document,
    parse_source_document,
)
from valuechain.financial_ie.multilingual.languages import get_language_pack, native_script_ratio
from valuechain.financial_ie.multilingual.prompts import (
    PROFILE_SYSTEMS,
    SIGNAL_SYSTEMS,
    EVIDENCE_REPAIR_SYSTEMS,
    build_evidence_repair_prompt,
    build_profile_prompt,
    build_signal_relation_prompt,
)
from valuechain.financial_ie.multilingual.quality import audit_record, review_rows, summarize
from valuechain.financial_ie.multilingual.retrieval import (
    select_profile_chunks,
    select_signal_chunks,
)
from valuechain.financial_ie.multilingual.schema import (
    SCHEMA_VERSION,
    empty_profile,
    normalize_profile,
    normalize_signal_relation_payload,
    refresh_relation_review,
)


EXPERIMENT_VERSION = "multilingual-financial-ie-experiment-v0.3"


@dataclass(frozen=True, slots=True)
class MultilingualExperimentConfig:
    output_dir: Path
    input_paths: tuple[Path, ...]
    model: str = "Qwen/Qwen3.6-35B-A3B"
    llm_base_url: str = "http://100.114.26.88:31969/v1"
    llm_api_key: str = "1969"
    concurrency: int = 4


class MultilingualExperiment:
    def __init__(self, config: MultilingualExperimentConfig) -> None:
        self.config = config
        self._parse_semaphore = asyncio.Semaphore(max(1, config.concurrency))
        self._write_lock = asyncio.Lock()

    async def run(self) -> dict[str, Any]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        records_path = self.config.output_dir / "records.jsonl"
        input_rows = [self._input_row(path) for path in self.config.input_paths]
        write_jsonl(self.config.output_dir / "input_manifest.jsonl", input_rows)
        completed = {
            record_key(row)
            for row in read_jsonl(records_path)
            if row.get("status") == "complete"
            and row.get("experiment_version") == EXPERIMENT_VERSION
            and row.get("extractor_model") == self.config.model
        }
        pending = [path for path in self.config.input_paths if path_key(path) not in completed]
        llm_config = AsyncLLMConfig(
            base_url=self.config.llm_base_url,
            api_key=self.config.llm_api_key,
            model=self.config.model,
            concurrency=self.config.concurrency,
            timeout_s=240,
            max_retries=3,
        )
        async with AsyncLLMClient(llm_config) as client:
            await asyncio.gather(
                *(self._execute(path, client, records_path) for path in pending)
            )
        latest: dict[str, dict[str, Any]] = {}
        requested = {path_key(path) for path in self.config.input_paths}
        for row in read_jsonl(records_path):
            key = record_key(row)
            if key in requested:
                latest[key] = row
        records = sorted(
            latest.values(),
            key=lambda row: (
                str((row.get("identity") or {}).get("language") or ""),
                str((row.get("identity") or {}).get("issuer_name") or ""),
            ),
        )
        write_jsonl(records_path, records)
        summary = materialize_outputs(self.config.output_dir, records, self.config)
        write_jsonl(records_path, records)
        return summary

    async def _execute(
        self,
        path: Path,
        client: AsyncLLMClient,
        records_path: Path,
    ) -> None:
        try:
            record = await self._process(path, client)
        except Exception as exc:  # noqa: BLE001
            record = {
                "status": "extraction_error",
                "error": f"{type(exc).__name__}: {exc}",
                "input_path": str(path),
                "identity": {"manifest_path": str(path)},
                "schema_version": SCHEMA_VERSION,
                "experiment_version": EXPERIMENT_VERSION,
                "profile": empty_profile(),
                "signals": [],
                "relations": [],
                "evidence_chunks": [],
            }
        async with self._write_lock:
            append_jsonl(records_path, record)

    async def _process(self, path: Path, client: AsyncLLMClient) -> dict[str, Any]:
        async with self._parse_semaphore:
            source, parsed = await asyncio.to_thread(self._parse, path)
        pack = get_language_pack(source.language)
        chunk_map = {chunk.chunk_id: chunk for chunk in parsed.chunks}
        profile_chunks = select_profile_chunks(parsed, pack)
        signal_chunks = select_signal_chunks(parsed, pack)
        raw_profile = ""
        profile_latency = 0.0
        profile_attempts = 0
        profile_parse_error = ""
        profile_payload: Any = {}
        if source.document_granularity == "periodic_report":
            response = await client.complete(
                PROFILE_SYSTEMS[pack.code],
                build_profile_prompt(source.issuer_name, source.title, pack.code, profile_chunks),
                max_tokens=1600,
            )
            raw_profile = response.content
            profile_latency = response.latency_s
            profile_attempts = response.attempts
            try:
                profile_payload = parse_json_payload(raw_profile)
            except ValueError as exc:
                profile_parse_error = str(exc)
        profile = normalize_profile(profile_payload, chunk_map)
        response = await client.complete(
            SIGNAL_SYSTEMS[pack.code],
            build_signal_relation_prompt(
                source.issuer_name,
                source.title,
                source.filing_type,
                pack.code,
                signal_chunks,
                profile,
            ),
            max_tokens=3400,
        )
        signal_parse_error = ""
        try:
            signal_payload = parse_json_payload(response.content)
        except ValueError as exc:
            signal_payload = {
                "signals": recover_partial_object_array(response.content, "signals"),
                "relations": recover_partial_object_array(response.content, "relations"),
            }
            signal_parse_error = str(exc)
        signals, relations = normalize_signal_relation_payload(
            signal_payload,
            chunk_map,
            source.issuer_name,
        )
        repair_items = collect_repair_items(profile, signals, relations, chunk_map)
        raw_repairs = ""
        repair_parse_error = ""
        repair_latency = 0.0
        repair_attempts = 0
        repair_stats = {"requested": 0, "accepted": 0, "rejected": 0}
        if repair_items:
            repair_response = await client.complete(
                EVIDENCE_REPAIR_SYSTEMS[pack.code],
                build_evidence_repair_prompt(repair_items),
                max_tokens=2200,
            )
            raw_repairs = repair_response.content
            repair_latency = repair_response.latency_s
            repair_attempts = repair_response.attempts
            try:
                repair_payload = parse_json_payload(raw_repairs)
            except ValueError as exc:
                repair_payload = {"repairs": recover_partial_object_array(raw_repairs, "repairs")}
                repair_parse_error = str(exc)
            repair_stats = apply_repairs(
                repair_payload,
                profile,
                signals,
                relations,
                chunk_map,
            )
        selected = {
            chunk.chunk_id: chunk
            for chunk in [*profile_chunks, *signal_chunks]
        }
        identity = source.to_dict()
        identity.pop("metadata", None)
        native_text = "\n".join(chunk.text for chunk in parsed.chunks)
        return {
            "status": "complete",
            "error": "",
            "schema_version": SCHEMA_VERSION,
            "experiment_version": EXPERIMENT_VERSION,
            "extractor_model": self.config.model,
            "extractor_endpoint": self.config.llm_base_url,
            "identity": identity,
            "profile": profile,
            "signals": signals,
            "relations": relations,
            "evidence_chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "section": chunk.section_hint,
                    "page": chunk.page,
                    "text": chunk.text,
                }
                for chunk in selected.values()
            ],
            "raw_model_outputs": {
                "profile": raw_profile,
                "signals_relations": response.content,
                "evidence_repairs": raw_repairs,
            },
            "diagnostics": {
                "parser_name": parsed.parser_name,
                "parser_version": parsed.parser_version,
                "parser_warnings": parsed.warnings,
                "source_character_count": parsed.source_character_count,
                "source_native_script_ratio": native_script_ratio(native_text, pack.code),
                "chunk_count": len(parsed.chunks),
                "profile_chunk_count": len(profile_chunks),
                "signal_chunk_count": len(signal_chunks),
                "profile_skipped_for_event": source.document_granularity == "event_disclosure",
                "profile_latency_s": profile_latency,
                "signal_relation_latency_s": response.latency_s,
                "profile_attempts": profile_attempts,
                "signal_relation_attempts": response.attempts,
                "evidence_repair_latency_s": repair_latency,
                "evidence_repair_attempts": repair_attempts,
                "evidence_repair_candidates": len(repair_items),
                "evidence_repair_stats": repair_stats,
                "profile_parse_error": profile_parse_error,
                "signal_relation_parse_error": signal_parse_error,
                "evidence_repair_parse_error": repair_parse_error,
            },
        }

    @staticmethod
    def _parse(path: Path) -> tuple[Any, Any]:
        source = load_source_document(path)
        return source, parse_source_document(source)

    @staticmethod
    def _input_row(path: Path) -> dict[str, Any]:
        try:
            source = load_source_document(path)
            return {"status": "ready", **source.to_dict()}
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "invalid",
                "manifest_path": str(path),
                "error": f"{type(exc).__name__}: {exc}",
            }


def materialize_outputs(
    output_dir: Path,
    records: list[dict[str, Any]],
    config: MultilingualExperimentConfig,
) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for record in records:
        identity = record.get("identity") or {}
        for relation in record.get("relations", []):
            refresh_relation_review(relation, str(identity.get("issuer_name") or ""))
        provenance = {
            key: identity.get(key)
            for key in (
                "source_id",
                "filing_id",
                "issuer_id",
                "issuer_name",
                "ticker",
                "language",
                "jurisdiction",
                "filing_type",
                "filed_at",
                "source_url",
            )
        }
        signals.extend({**provenance, **item} for item in record.get("signals", []))
        relations.extend({**provenance, **item} for item in record.get("relations", []))
        issues.extend(audit_record(record))
    write_jsonl(output_dir / "signals.jsonl", signals)
    write_jsonl(output_dir / "relations.jsonl", relations)
    write_csv(output_dir / "quality_issues.csv", issues)
    write_csv(output_dir / "human_review.csv", review_rows(records))
    summary = summarize(records, issues)
    summary.update(
        {
            "experiment_version": EXPERIMENT_VERSION,
            "model": config.model,
            "llm_base_url": config.llm_base_url,
            "concurrency": config.concurrency,
            "completed_at": datetime.now(UTC).isoformat(),
            "artifacts": [
                "input_manifest.jsonl",
                "records.jsonl",
                "signals.jsonl",
                "relations.jsonl",
                "quality_issues.csv",
                "human_review.csv",
                "run_summary.json",
            ],
        }
    )
    (output_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def path_key(path: Path) -> str:
    return str(path)


def record_key(record: dict[str, Any]) -> str:
    identity = record.get("identity") or {}
    return str(identity.get("manifest_path") or record.get("input_path") or "")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
