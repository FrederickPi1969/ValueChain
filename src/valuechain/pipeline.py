from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from valuechain.aggregation import aggregate_edges, bottleneck_candidates
from valuechain.config import Settings, ensure_dirs
from valuechain.dashboard import render_dashboard
from valuechain.edge_quality import denoise_relation_evidence
from valuechain.embeddings import EmbeddingConfig, OpenAIEmbeddingClient, embedding_merge_relation_evidence
from valuechain.entity_resolution import EntityResolver
from valuechain.filing_parser import parse_sections, segment_passages
from valuechain.io_utils import write_csv, write_jsonl, write_json
from valuechain.llm_client import LLMConfig, OpenAICompatibleClient
from valuechain.models import Company, FilingRecord, GraphEdge, Passage, RelationEvidence
from valuechain.planning import build_execution_plan
from valuechain.postgres import write_run_to_postgres
from valuechain.relation_llm import HybridRelationExtractor, LLMRelationExtractor
from valuechain.relation_rules import RuleBasedRelationExtractor
from valuechain.relevance import filter_candidates
from valuechain.run_registry import (
    copy_latest_dashboard,
    copy_latest_processed_outputs,
    make_run_id,
    normalize_run_id,
    render_run_index,
    update_run_registry,
)
from valuechain.sec_client import SECClient
from valuechain.universe import read_universe, summarize_universe
from valuechain.yahoo_enrichment import fetch_yahoo_snapshot


@dataclass(frozen=True)
class PipelineOptions:
    universe_path: Path
    tickers: list[str] | None = None
    roles: list[str] | None = None
    max_priority: int | None = None
    limit_companies: int | None = None
    forms: tuple[str, ...] = ("10-K", "10-Q", "8-K", "20-F", "6-K")
    max_filings_per_company: int = 2
    filing_date_from: str = ""
    filing_date_to: str = ""
    extractor: str = "rules"
    min_relevance_score: float = 2.0
    skip_yahoo: bool = False
    run_id: str = ""
    run_label: str = ""
    write_postgres: bool = False
    postgres_url: str = ""
    llm_concurrency: int = 4
    embedding_merge: bool = False
    embedding_threshold: float = 0.92


@dataclass
class PipelineResult:
    companies: list[Company]
    filings: list[FilingRecord]
    passages: list[Passage]
    candidate_passages: list[Passage]
    evidence: list[RelationEvidence]
    edges: list[GraphEdge]
    yahoo_rows: list[dict]
    dashboard_path: Path
    run_id: str
    index_path: Path


def run_pipeline(settings: Settings, options: PipelineOptions) -> PipelineResult:
    ensure_dirs(settings)
    run_id = normalize_run_id(options.run_id) if options.run_id else make_run_id("valuechain")
    run_processed_dir = settings.processed_dir / "runs" / run_id
    run_report_dir = settings.reports_dir / "runs" / run_id
    run_processed_dir.mkdir(parents=True, exist_ok=True)
    run_report_dir.mkdir(parents=True, exist_ok=True)
    companies = read_universe(
        options.universe_path,
        tickers=options.tickers,
        roles=options.roles,
        max_priority=options.max_priority,
        limit=options.limit_companies,
    )
    write_json(
        run_processed_dir / "input_plan.json",
        build_execution_plan(
            companies,
            forms=options.forms,
            max_filings_per_company=options.max_filings_per_company,
            filing_date_from=options.filing_date_from,
            filing_date_to=options.filing_date_to,
        ).to_dict(),
    )
    sec_client = SECClient(
        user_agent=settings.sec_user_agent,
        requests_per_second=settings.sec_rps,
        proxies=settings.proxies,
    )
    resolved_companies = sec_client.resolve_companies(companies)
    write_csv(
        run_processed_dir / "company_universe_resolved.csv",
        [company.to_dict() for company in resolved_companies],
        fieldnames=["ticker", "company_name", "role", "priority", "notes", "cik", "exchange"],
    )

    filings = discover_and_download_filings(sec_client, resolved_companies, settings, options)
    write_csv(run_processed_dir / "filing_manifest.csv", [filing.to_dict() for filing in filings])

    passages = parse_all_passages(filings)
    candidate_passages = filter_candidates(passages, min_score=options.min_relevance_score)
    write_jsonl(run_processed_dir / "passages.jsonl", [passage.to_dict() for passage in passages])
    write_jsonl(
        run_processed_dir / "candidate_passages.jsonl",
        [passage.to_dict() for passage in candidate_passages],
    )

    extractor = build_extractor(settings, options, resolved_companies)
    raw_evidence = extract_relations(
        candidate_passages,
        extractor,
        concurrency=max(1, options.llm_concurrency),
    )
    write_jsonl(
        run_processed_dir / "relation_evidence_raw.jsonl",
        [record.to_dict() for record in raw_evidence],
    )

    evidence, merge_diagnostics = denoise_relation_evidence(raw_evidence)
    embedding_diagnostics: list[dict[str, object]] = []
    if options.embedding_merge:
        evidence, embedding_diagnostics = apply_embedding_merge(settings, options, evidence)
        if embedding_diagnostics:
            evidence, post_embedding_diagnostics = denoise_relation_evidence(evidence)
            merge_diagnostics.extend(post_embedding_diagnostics)
    write_csv(run_processed_dir / "embedding_merge_diagnostics.csv", embedding_diagnostics)
    write_csv(run_processed_dir / "merge_diagnostics.csv", merge_diagnostics)
    write_jsonl(run_processed_dir / "relation_evidence.jsonl", [record.to_dict() for record in evidence])

    edges = aggregate_edges(evidence, apply_quality_gate=False)
    write_csv(run_processed_dir / "graph_edges.csv", [edge.to_dict() for edge in edges])
    write_csv(run_processed_dir / "bottleneck_candidates.csv", bottleneck_candidates(edges))
    write_validation_sample(run_processed_dir / "validation_sample.csv", evidence)

    yahoo_rows = [] if options.skip_yahoo else fetch_yahoo_snapshot(resolved_companies)
    if yahoo_rows:
        write_csv(run_processed_dir / "yahoo_snapshot.csv", yahoo_rows)

    dashboard_path = run_report_dir / "dashboard.html"
    dashboard_data = render_dashboard(dashboard_path, edges, evidence, yahoo_rows)
    write_json(run_report_dir / "dashboard-data.json", dashboard_data)
    summary = build_run_summary(
        settings,
        options,
        run_id,
        run_processed_dir,
        dashboard_path,
        resolved_companies,
        filings,
        passages,
        candidate_passages,
        evidence,
        edges,
        raw_evidence_count=len(raw_evidence),
        merge_diagnostics=merge_diagnostics,
    )
    write_json(run_processed_dir / "run_summary.json", summary)
    if options.write_postgres:
        write_run_to_postgres(
            database_url=options.postgres_url or settings.database_url,
            run_id=run_id,
            summary=summary,
            companies=resolved_companies,
            filings=filings,
            passages=passages,
            candidate_passages=candidate_passages,
            evidence=evidence,
            edges=edges,
        )
    update_run_registry(
        settings,
        run_id=run_id,
        run_label=options.run_label,
        summary=summary,
        dashboard_path=dashboard_path,
        processed_dir=run_processed_dir,
    )
    copy_latest_dashboard(settings, dashboard_path)
    copy_latest_processed_outputs(run_processed_dir, settings.processed_dir)
    index_path = render_run_index(settings)

    return PipelineResult(
        companies=resolved_companies,
        filings=filings,
        passages=passages,
        candidate_passages=candidate_passages,
        evidence=evidence,
        edges=edges,
        yahoo_rows=yahoo_rows,
        dashboard_path=dashboard_path,
        run_id=run_id,
        index_path=index_path,
    )


def discover_and_download_filings(
    sec_client: SECClient,
    companies: list[Company],
    settings: Settings,
    options: PipelineOptions,
) -> list[FilingRecord]:
    forms = set(options.forms)
    filings: list[FilingRecord] = []
    for company in companies:
        company_filings = sec_client.discover_filings(
            company,
            forms=forms,
            max_filings=options.max_filings_per_company,
            filing_date_from=options.filing_date_from,
            filing_date_to=options.filing_date_to,
        )
        for filing in company_filings:
            filings.append(sec_client.download_primary_document(filing, settings.raw_dir))
    return filings


def parse_all_passages(filings: list[FilingRecord]) -> list[Passage]:
    passages: list[Passage] = []
    for filing in filings:
        for section in parse_sections(filing):
            passages.extend(segment_passages(section))
    return passages


def build_extractor(settings: Settings, options: PipelineOptions, companies: list[Company]):
    resolver = EntityResolver(companies)
    rules = RuleBasedRelationExtractor(resolver)
    if options.extractor == "rules":
        return rules
    llm_client = OpenAICompatibleClient(
        LLMConfig(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.extraction_model,
            report_url=settings.llm_report_url,
            proxy_url=settings.https_proxy or settings.http_proxy,
            max_connections=max(4, options.llm_concurrency * 2),
            max_keepalive_connections=max(2, options.llm_concurrency),
        )
    )
    llm = LLMRelationExtractor(llm_client, model_version=settings.extraction_model)
    if options.extractor == "llm":
        return llm
    if options.extractor == "hybrid":
        return HybridRelationExtractor(rules, llm)
    raise ValueError(f"Unknown extractor: {options.extractor}")


def extract_relations(
    candidate_passages: list[Passage],
    extractor,
    concurrency: int = 4,
) -> list[RelationEvidence]:
    if hasattr(extractor, "extract_async"):
        return asyncio.run(extract_relations_async(candidate_passages, extractor, concurrency=concurrency))
    records: list[RelationEvidence] = []
    for passage in candidate_passages:
        records.extend(extractor.extract(passage))
    return records


async def extract_relations_async(
    candidate_passages: list[Passage],
    extractor,
    concurrency: int = 4,
) -> list[RelationEvidence]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def extract_one(passage: Passage) -> list[RelationEvidence]:
        async with semaphore:
            return await extractor.extract_async(passage)

    try:
        batches = await asyncio.gather(*(extract_one(passage) for passage in candidate_passages))
    finally:
        if hasattr(extractor, "aclose"):
            await extractor.aclose()
    return [record for batch in batches for record in batch]


def apply_embedding_merge(
    settings: Settings,
    options: PipelineOptions,
    evidence: list[RelationEvidence],
) -> tuple[list[RelationEvidence], list[dict[str, object]]]:
    client = OpenAIEmbeddingClient(
        EmbeddingConfig(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.embedding_model,
            proxy_url=settings.https_proxy or settings.http_proxy,
        )
    )
    try:
        return embedding_merge_relation_evidence(
            evidence,
            client,
            threshold=options.embedding_threshold,
        )
    except Exception as exc:
        return evidence, [
            {
                "action": "error",
                "reason": f"{type(exc).__name__}: {exc}",
                "model": settings.embedding_model,
                "threshold": options.embedding_threshold,
            }
        ]


def write_validation_sample(path: Path, evidence: list[RelationEvidence], limit: int = 120) -> None:
    rows = []
    for record in sorted(evidence, key=lambda item: (item.subject, item.relation_type, item.passage_id))[:limit]:
        rows.append(
            {
                "gold_relation_present": "",
                "gold_relation_type": "",
                "gold_modality": "",
                "review_notes": "",
                "subject": record.subject,
                "object": record.object,
                "relation_type": record.relation_type,
                "modality": record.modality,
                "confidence_score": record.confidence_score,
                "form": record.form,
                "filing_date": record.filing_date,
                "section": record.source_section,
                "passage_id": record.passage_id,
                "evidence_text": record.evidence_text,
                "source_document_url": record.source_document_url,
            }
        )
    write_csv(path, rows)


def build_run_summary(
    settings: Settings,
    options: PipelineOptions,
    run_id: str,
    processed_dir: Path,
    dashboard_path: Path,
    companies: list[Company],
    filings: list[FilingRecord],
    passages: list[Passage],
    candidate_passages: list[Passage],
    evidence: list[RelationEvidence],
    edges: list[GraphEdge],
    raw_evidence_count: int = 0,
    merge_diagnostics: list[dict[str, object]] | None = None,
) -> dict:
    dropped_count = sum(1 for row in merge_diagnostics or [] if row.get("action") == "drop")
    return {
        "run_id": run_id,
        "run_label": options.run_label or run_id,
        "options": {
            "tickers": options.tickers,
            "roles": options.roles,
            "max_priority": options.max_priority,
            "limit_companies": options.limit_companies,
            "forms": list(options.forms),
            "max_filings_per_company": options.max_filings_per_company,
            "filing_date_from": options.filing_date_from,
            "filing_date_to": options.filing_date_to,
            "extractor": options.extractor,
            "min_relevance_score": options.min_relevance_score,
            "skip_yahoo": options.skip_yahoo,
            "run_id": run_id,
            "run_label": options.run_label,
            "write_postgres": options.write_postgres,
            "llm_concurrency": options.llm_concurrency,
            "embedding_merge": options.embedding_merge,
            "embedding_threshold": options.embedding_threshold,
            "extraction_model": settings.extraction_model,
            "complex_model": settings.complex_model,
            "embedding_model": settings.embedding_model,
        },
        "counts": {
            "companies": len(companies),
            "roles": summarize_universe(companies)["role_counts"],
            "filings": len(filings),
            "passages": len(passages),
            "candidate_passages": len(candidate_passages),
            "relation_evidence_raw": raw_evidence_count or len(evidence),
            "relation_evidence_dropped": dropped_count,
            "relation_evidence": len(evidence),
            "graph_edges": len(edges),
        },
        "outputs": {
            "company_universe": str(processed_dir / "company_universe_resolved.csv"),
            "input_plan": str(processed_dir / "input_plan.json"),
            "filing_manifest": str(processed_dir / "filing_manifest.csv"),
            "relation_evidence_raw": str(processed_dir / "relation_evidence_raw.jsonl"),
            "relation_evidence": str(processed_dir / "relation_evidence.jsonl"),
            "merge_diagnostics": str(processed_dir / "merge_diagnostics.csv"),
            "embedding_merge_diagnostics": str(processed_dir / "embedding_merge_diagnostics.csv"),
            "graph_edges": str(processed_dir / "graph_edges.csv"),
            "validation_sample": str(processed_dir / "validation_sample.csv"),
            "dashboard": str(dashboard_path),
            "dashboard_data": str(dashboard_path.parent / "dashboard-data.json"),
        },
    }
