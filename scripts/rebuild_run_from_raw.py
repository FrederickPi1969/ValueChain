from __future__ import annotations

import argparse
import csv
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from valuechain.aggregation import aggregate_edges, bottleneck_candidates
from valuechain.config import Settings
from valuechain.dashboard import render_dashboard
from valuechain.edge_quality import denoise_relation_evidence
from valuechain.embeddings import EmbeddingConfig, OpenAIEmbeddingClient, embedding_merge_relation_evidence
from valuechain.io_utils import read_jsonl, write_csv, write_json, write_jsonl
from valuechain.models import Company, FilingRecord, Passage, RelationEvidence, SourceDocument
from valuechain.pipeline import PipelineOptions, build_run_summary, write_validation_sample
from valuechain.postgres import write_run_to_postgres
from valuechain.run_registry import (
    copy_latest_dashboard,
    copy_latest_processed_outputs,
    render_run_index,
    update_run_registry,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild downstream run artifacts from cached raw evidence.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--no-postgres", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    processed_dir = settings.processed_dir / "runs" / args.run_id
    report_dir = settings.reports_dir / "runs" / args.run_id
    summary_path = processed_dir / "run_summary.json"
    if not summary_path.exists():
        raise SystemExit(f"Missing run summary: {summary_path}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    opts = summary.get("options", {})
    options = options_from_summary(settings, args.run_id, opts, write_postgres=not args.no_postgres)

    companies = read_csv_dataclass(processed_dir / "company_universe_resolved.csv", Company)
    filings = read_csv_dataclass(processed_dir / "filing_manifest.csv", FilingRecord)
    source_documents = read_csv_dataclass(processed_dir / "source_document_manifest.csv", SourceDocument)
    passages = [coerce_dataclass(Passage, row) for row in read_jsonl(processed_dir / "passages.jsonl")]
    candidate_passages = [
        coerce_dataclass(Passage, row) for row in read_jsonl(processed_dir / "candidate_passages.jsonl")
    ]
    raw_evidence = [
        coerce_dataclass(RelationEvidence, row) for row in read_jsonl(processed_dir / "relation_evidence_raw.jsonl")
    ]

    evidence, merge_diagnostics = denoise_relation_evidence(raw_evidence)
    evidence, embedding_diagnostics = rebuild_embedding_merge(settings, options, evidence)
    if any(row.get("action") == "merge" for row in embedding_diagnostics):
        evidence, post_embedding_diagnostics = denoise_relation_evidence(evidence)
        merge_diagnostics.extend(post_embedding_diagnostics)

    write_csv(processed_dir / "embedding_merge_diagnostics.csv", embedding_diagnostics)
    write_csv(processed_dir / "merge_diagnostics.csv", merge_diagnostics)
    write_jsonl(processed_dir / "relation_evidence.jsonl", [record.to_dict() for record in evidence])

    edges = aggregate_edges(evidence, apply_quality_gate=False)
    write_csv(processed_dir / "graph_edges.csv", [edge.to_dict() for edge in edges])
    write_csv(processed_dir / "bottleneck_candidates.csv", bottleneck_candidates(edges))
    write_validation_sample(processed_dir / "validation_sample.csv", evidence)

    dashboard_path = report_dir / "dashboard.html"
    dashboard_data = render_dashboard(
        dashboard_path,
        edges,
        evidence,
        [],
        companies,
        filings=filings,
        source_documents=source_documents,
        passages=passages,
        candidate_passages=candidate_passages,
    )
    write_json(report_dir / "dashboard-data.json", dashboard_data)

    new_summary = build_run_summary(
        settings,
        options,
        args.run_id,
        processed_dir,
        dashboard_path,
        companies,
        filings,
        source_documents,
        passages,
        candidate_passages,
        evidence,
        edges,
        raw_evidence_count=len(raw_evidence),
        merge_diagnostics=merge_diagnostics,
    )
    write_json(processed_dir / "run_summary.json", new_summary)

    if options.write_postgres:
        write_run_to_postgres(
            database_url=options.postgres_url or settings.database_url,
            run_id=args.run_id,
            summary=new_summary,
            companies=companies,
            filings=filings,
            source_documents=source_documents,
            passages=passages,
            candidate_passages=candidate_passages,
            evidence=evidence,
            edges=edges,
        )

    update_run_registry(settings, args.run_id, options.run_label, new_summary, dashboard_path, processed_dir)
    copy_latest_dashboard(settings, dashboard_path)
    copy_latest_processed_outputs(processed_dir, settings.processed_dir)
    render_run_index(settings)
    print(
        "rebuilt",
        f"run_id={args.run_id}",
        f"raw_evidence={len(raw_evidence)}",
        f"evidence={len(evidence)}",
        f"edges={len(edges)}",
        f"embedding_diag={len(embedding_diagnostics)}",
    )


def rebuild_embedding_merge(
    settings: Settings,
    options: PipelineOptions,
    evidence: list[RelationEvidence],
) -> tuple[list[RelationEvidence], list[dict[str, object]]]:
    if not options.embedding_merge:
        return evidence, []
    client = OpenAIEmbeddingClient(
        EmbeddingConfig(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.embedding_model,
            proxy_url=settings.https_proxy or settings.http_proxy,
        )
    )
    return embedding_merge_relation_evidence(evidence, client, threshold=options.embedding_threshold)


def read_csv_dataclass(path: Path, cls):
    with path.open(newline="", encoding="utf-8") as handle:
        return [coerce_dataclass(cls, row) for row in csv.DictReader(handle)]


def coerce_dataclass(cls, row: dict[str, Any]):
    allowed = {field.name for field in fields(cls)}
    data = {key: value for key, value in row.items() if key in allowed}
    for key in ["priority", "paragraph_offset"]:
        if key in data and data[key] != "":
            data[key] = int(data[key])
    for key in ["relevance_score", "confidence_score"]:
        if key in data and data[key] != "":
            data[key] = float(data[key])
    if "is_primary" in data:
        data["is_primary"] = str(data["is_primary"]).lower() == "true"
    if cls is Passage and not isinstance(data.get("relevance_terms"), list):
        data["relevance_terms"] = []
    return cls(**data)


def options_from_summary(
    settings: Settings,
    run_id: str,
    opts: dict[str, Any],
    write_postgres: bool,
) -> PipelineOptions:
    return PipelineOptions(
        universe_path=settings.root_dir / "config" / "company_universe.csv",
        tickers=opts.get("tickers"),
        roles=opts.get("roles"),
        max_priority=opts.get("max_priority"),
        limit_companies=opts.get("limit_companies"),
        forms=tuple(opts.get("forms") or ("10-K", "10-Q", "8-K", "20-F", "6-K")),
        max_filings_per_company=int(opts.get("max_filings_per_company") or 2),
        filing_selection=opts.get("filing_selection") or "form_balanced",
        filing_date_from=opts.get("filing_date_from") or "",
        filing_date_to=opts.get("filing_date_to") or "",
        extractor=opts.get("extractor") or "hybrid",
        min_relevance_score=float(opts.get("min_relevance_score") or 1.8),
        skip_yahoo=bool(opts.get("skip_yahoo")),
        run_id=run_id,
        run_label=opts.get("run_label") or run_id,
        write_postgres=write_postgres and bool(opts.get("write_postgres")),
        llm_concurrency=int(opts.get("llm_concurrency") or 6),
        embedding_merge=bool(opts.get("embedding_merge")),
        embedding_threshold=float(opts.get("embedding_threshold") or 0.92),
        include_exhibits=bool(opts.get("include_exhibits")),
        exhibit_types=tuple(opts.get("exhibit_types") or ("EX-10", "EX-21", "EX-99", "EX-99.1")),
        max_exhibits_per_filing=int(opts.get("max_exhibits_per_filing") or 6),
    )


if __name__ == "__main__":
    main()
