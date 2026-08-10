from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from valuechain.aggregation import aggregate_edges, bottleneck_candidates
from valuechain.canonicalization import build_canonical_layer
from valuechain.config import MAX_LLM_CONCURRENCY, Settings, ensure_dirs
from valuechain.dashboard import render_dashboard
from valuechain.document_consistency import reconcile_document_mentions, reconcile_document_relations
from valuechain.edge_quality import denoise_relation_evidence
from valuechain.embeddings import EmbeddingConfig, OpenAIEmbeddingClient, embedding_merge_relation_evidence
from valuechain.entity_resolution import EntityResolver
from valuechain.filing_parser import parse_sections, segment_passages
from valuechain.io_utils import read_jsonl, write_csv, write_jsonl, write_json
from valuechain.llm_client import LLMConfig, OpenAICompatibleClient
from valuechain.models import Company, FilingRecord, GraphEdge, MentionCluster, Passage, RelationEvidence, SourceDocument
from valuechain.mention_layer import build_alias_review_queue, build_entity_triage, extract_passage_mentions
from valuechain.mention_constrained_extraction import MentionConstrainedExtractor
from valuechain.resolution_records import build_resolution_records
from valuechain.planning import build_execution_plan
from valuechain.postgres import write_run_to_postgres
from valuechain.human_review import inherit_prior_reviews
from valuechain.industry_expansion import ExpansionConfig, build_industry_expansion
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
    filing_selection: str = "form_balanced"
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
    include_exhibits: bool = True
    exhibit_types: tuple[str, ...] = ("EX-10", "EX-21", "EX-99", "EX-99.1")
    max_exhibits_per_filing: int = 8


@dataclass
class PipelineResult:
    companies: list[Company]
    filings: list[FilingRecord]
    source_documents: list[SourceDocument]
    passages: list[Passage]
    candidate_passages: list[Passage]
    evidence: list[RelationEvidence]
    edges: list[GraphEdge]
    yahoo_rows: list[dict]
    dashboard_path: Path
    run_id: str
    index_path: Path


def run_pipeline(settings: Settings, options: PipelineOptions) -> PipelineResult:
    if not 1 <= options.llm_concurrency <= MAX_LLM_CONCURRENCY:
        raise ValueError(
            f"--llm-concurrency must be between 1 and {MAX_LLM_CONCURRENCY} to protect the Local LLM service"
        )
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
            filing_selection=options.filing_selection,
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

    filings, source_documents = discover_and_download_filings(sec_client, resolved_companies, settings, options)
    write_csv(run_processed_dir / "filing_manifest.csv", [filing.to_dict() for filing in filings])
    write_csv(
        run_processed_dir / "source_document_manifest.csv",
        [document.to_dict() for document in source_documents],
    )

    passages = parse_all_passages(source_documents)
    candidate_passages = filter_candidates(passages, min_score=options.min_relevance_score)
    entity_mentions = extract_passage_mentions(passages, resolved_companies)
    entity_mentions, mention_clusters, document_entity_diagnostics = reconcile_document_mentions(
        passages,
        entity_mentions,
    )
    passages_by_id = {row.passage_id: row for row in passages}
    entity_triage = build_entity_triage(entity_mentions, mention_clusters, passages_by_id)
    alias_review_queue = build_alias_review_queue(entity_mentions, mention_clusters, passages_by_id)
    write_jsonl(run_processed_dir / "passages.jsonl", [passage.to_dict() for passage in passages])
    write_jsonl(run_processed_dir / "entity_mentions.jsonl", [mention.to_dict() for mention in entity_mentions])
    write_jsonl(run_processed_dir / "mention_clusters.jsonl", [cluster.to_dict() for cluster in mention_clusters])
    write_csv(run_processed_dir / "entity_triage.csv", entity_triage)
    write_csv(run_processed_dir / "alias_review_queue.csv", alias_review_queue)
    write_csv(
        run_processed_dir / "document_entity_consistency_diagnostics.csv",
        document_entity_diagnostics,
    )
    write_jsonl(
        run_processed_dir / "candidate_passages.jsonl",
        [passage.to_dict() for passage in candidate_passages],
    )

    base_extractor = build_extractor(settings, options, resolved_companies)
    mentions_by_passage: dict[str, list] = {}
    for mention in entity_mentions:
        mentions_by_passage.setdefault(mention.passage_id, []).append(mention)
    extractor = MentionConstrainedExtractor(base_extractor, mentions_by_passage)
    raw_evidence = extract_relations(
        candidate_passages,
        extractor,
        concurrency=max(1, options.llm_concurrency),
    )
    write_jsonl(
        run_processed_dir / "relation_evidence_raw.jsonl",
        [record.to_dict() for record in raw_evidence],
    )
    write_csv(run_processed_dir / "mention_constraint_diagnostics.csv", extractor.diagnostics)

    evidence, merge_diagnostics = denoise_relation_evidence(raw_evidence)
    embedding_diagnostics: list[dict[str, object]] = []
    if options.embedding_merge:
        evidence, embedding_diagnostics = apply_embedding_merge(settings, options, evidence)
        if any(row.get("action") == "merge" for row in embedding_diagnostics):
            evidence, post_embedding_diagnostics = denoise_relation_evidence(evidence)
            merge_diagnostics.extend(post_embedding_diagnostics)
    evidence, document_relation_diagnostics = reconcile_document_relations(evidence)
    merge_diagnostics.extend(document_relation_diagnostics)
    write_csv(run_processed_dir / "embedding_merge_diagnostics.csv", embedding_diagnostics)
    write_csv(run_processed_dir / "merge_diagnostics.csv", merge_diagnostics)
    write_jsonl(run_processed_dir / "relation_evidence.jsonl", [record.to_dict() for record in evidence])
    resolution_records = build_resolution_records(entity_mentions, mention_clusters, passages, evidence)
    write_jsonl(run_processed_dir / "entity_resolution_records.jsonl", resolution_records)
    write_csv(run_processed_dir / "entity_resolution_review.csv", resolution_records)

    # A changed canonical id may inherit only an explicit human decision.
    # LLM and verification outcomes are keyed audit records and must be replayed
    # by the audit ledger, never heuristically matched across a rerun.
    prior_reviewed = [
        row for row in read_jsonl(run_processed_dir / "canonical_relationships_reviewed.jsonl")
        if row.get("decision_source") == "human_review"
    ]
    canonical_entities, canonical_relationships, canonical_diagnostics = build_canonical_layer(
        resolved_companies, evidence
    )
    if prior_reviewed:
        canonical_relationships = inherit_prior_reviews(canonical_relationships, prior_reviewed)
    write_jsonl(run_processed_dir / "canonical_entities.jsonl", canonical_entities)
    write_jsonl(run_processed_dir / "canonical_relationships.jsonl", canonical_relationships)
    write_csv(run_processed_dir / "canonicalization_diagnostics.csv", canonical_diagnostics)
    industry_expansion = build_industry_expansion(
        resolved_companies,
        canonical_entities,
        canonical_relationships,
        ExpansionConfig(
            industry="run-universe",
            seeds=[company.company_name for company in resolved_companies],
            max_seeds=max(50, len(resolved_companies)),
            max_nodes=5_000,
            max_edges=15_000,
        ),
    )
    write_json(run_processed_dir / "industry_expansion.json", industry_expansion)

    edges = aggregate_edges(evidence, apply_quality_gate=False)
    write_csv(run_processed_dir / "graph_edges.csv", [edge.to_dict() for edge in edges])
    write_csv(run_processed_dir / "bottleneck_candidates.csv", bottleneck_candidates(edges))
    write_validation_sample(run_processed_dir / "validation_sample.csv", evidence)

    yahoo_rows = [] if options.skip_yahoo else fetch_yahoo_snapshot(resolved_companies)
    if yahoo_rows:
        write_csv(run_processed_dir / "yahoo_snapshot.csv", yahoo_rows)

    dashboard_path = run_report_dir / "dashboard.html"
    dashboard_data = render_dashboard(
        dashboard_path,
        edges,
        evidence,
        yahoo_rows,
        resolved_companies,
        filings=filings,
        source_documents=source_documents,
        passages=passages,
        candidate_passages=candidate_passages,
        canonical_entities=canonical_entities,
        canonical_relationships=canonical_relationships,
        canonicalization_diagnostics=canonical_diagnostics,
        industry_expansion=industry_expansion,
    )
    write_json(run_report_dir / "dashboard-data.json", dashboard_data)
    summary = build_run_summary(
        settings,
        options,
        run_id,
        run_processed_dir,
        dashboard_path,
        resolved_companies,
        filings,
        source_documents,
        passages,
        candidate_passages,
        evidence,
        edges,
        raw_evidence_count=len(raw_evidence),
        merge_diagnostics=merge_diagnostics,
        canonical_entities=canonical_entities,
        canonical_relationships=canonical_relationships,
        canonical_diagnostics=canonical_diagnostics,
        entity_mentions=entity_mentions,
        mention_clusters=mention_clusters,
        alias_review_queue=alias_review_queue,
        entity_triage=entity_triage,
        resolution_records=resolution_records,
        mention_constraint_diagnostics=extractor.diagnostics,
    )
    write_json(run_processed_dir / "run_summary.json", summary)
    if options.write_postgres:
        write_run_to_postgres(
            database_url=options.postgres_url or settings.database_url,
            run_id=run_id,
            summary=summary,
            companies=resolved_companies,
            filings=filings,
            source_documents=source_documents,
            passages=passages,
            candidate_passages=candidate_passages,
            evidence=evidence,
            edges=edges,
            canonical_entities=canonical_entities,
            canonical_relationships=canonical_relationships,
            entity_mentions=entity_mentions,
            mention_clusters=mention_clusters,
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
        source_documents=source_documents,
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
) -> tuple[list[FilingRecord], list[SourceDocument]]:
    forms = set(options.forms)
    filings: list[FilingRecord] = []
    source_documents: list[SourceDocument] = []
    for company in companies:
        company_filings = sec_client.discover_filings(
            company,
            forms=forms,
            max_filings=options.max_filings_per_company,
            filing_date_from=options.filing_date_from,
            filing_date_to=options.filing_date_to,
            selection=options.filing_selection,
        )
        for filing in company_filings:
            documents = sec_client.download_source_documents(
                filing,
                settings.raw_dir,
                include_exhibits=options.include_exhibits,
                exhibit_types=options.exhibit_types,
                max_exhibits_per_filing=options.max_exhibits_per_filing,
            )
            filings.append(filing)
            source_documents.extend(documents)
    return filings, source_documents


def parse_all_passages(source_documents: list[SourceDocument]) -> list[Passage]:
    passages: list[Passage] = []
    for source_document in source_documents:
        for section in parse_sections(source_document):
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
            max_connections=options.llm_concurrency,
            max_keepalive_connections=min(8, options.llm_concurrency),
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
                "source_document": record.source_document,
                "source_document_type": record.source_document_type,
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
    source_documents: list[SourceDocument],
    passages: list[Passage],
    candidate_passages: list[Passage],
    evidence: list[RelationEvidence],
    edges: list[GraphEdge],
    raw_evidence_count: int = 0,
    merge_diagnostics: list[dict[str, object]] | None = None,
    canonical_entities: list[dict[str, object]] | None = None,
    canonical_relationships: list[dict[str, object]] | None = None,
    canonical_diagnostics: list[dict[str, object]] | None = None,
    entity_mentions: list[object] | None = None,
    mention_clusters: list[object] | None = None,
    mention_constraint_diagnostics: list[dict[str, object]] | None = None,
    alias_review_queue: list[dict[str, object]] | None = None,
    entity_triage: list[dict[str, object]] | None = None,
    resolution_records: list[dict[str, object]] | None = None,
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
            "filing_selection": options.filing_selection,
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
            "include_exhibits": options.include_exhibits,
            "exhibit_types": list(options.exhibit_types),
            "max_exhibits_per_filing": options.max_exhibits_per_filing,
            "extraction_model": settings.extraction_model,
            "complex_model": settings.complex_model,
            "embedding_model": settings.embedding_model,
        },
        "counts": {
            "companies": len(companies),
            "roles": summarize_universe(companies)["role_counts"],
            "filings": len(filings),
            "source_documents": len(source_documents),
            "exhibit_documents": sum(1 for document in source_documents if not document.is_primary),
            "passages": len(passages),
            "entity_mentions": len(entity_mentions or []),
            "mention_clusters": len(mention_clusters or []),
            "alias_review_candidates": len(alias_review_queue or []),
            "entity_triage_rows": len(entity_triage or []),
            "entity_resolution_records": len(resolution_records or []),
            "relation_evidence_unmentioned_named_dropped": len(mention_constraint_diagnostics or []),
            "candidate_passages": len(candidate_passages),
            "relation_evidence_raw": raw_evidence_count or len(evidence),
            "relation_evidence_dropped": dropped_count,
            "relation_evidence": len(evidence),
            "graph_edges": len(edges),
            "canonical_entities": len(canonical_entities or []),
            "canonical_relationships": len(canonical_relationships or []),
            "canonicalization_excluded": sum(
                1 for row in canonical_diagnostics or [] if row.get("status") != "canonicalized"
            ),
        },
        "outputs": {
            "company_universe": str(processed_dir / "company_universe_resolved.csv"),
            "input_plan": str(processed_dir / "input_plan.json"),
            "filing_manifest": str(processed_dir / "filing_manifest.csv"),
            "source_document_manifest": str(processed_dir / "source_document_manifest.csv"),
            "entity_mentions": str(processed_dir / "entity_mentions.jsonl"),
            "mention_clusters": str(processed_dir / "mention_clusters.jsonl"),
            "mention_constraint_diagnostics": str(processed_dir / "mention_constraint_diagnostics.csv"),
            "alias_review_queue": str(processed_dir / "alias_review_queue.csv"),
            "entity_triage": str(processed_dir / "entity_triage.csv"),
            "entity_resolution_records": str(processed_dir / "entity_resolution_records.jsonl"),
            "entity_resolution_review": str(processed_dir / "entity_resolution_review.csv"),
            "relation_evidence_raw": str(processed_dir / "relation_evidence_raw.jsonl"),
            "relation_evidence": str(processed_dir / "relation_evidence.jsonl"),
            "merge_diagnostics": str(processed_dir / "merge_diagnostics.csv"),
            "embedding_merge_diagnostics": str(processed_dir / "embedding_merge_diagnostics.csv"),
            "graph_edges": str(processed_dir / "graph_edges.csv"),
            "canonical_entities": str(processed_dir / "canonical_entities.jsonl"),
            "canonical_relationships": str(processed_dir / "canonical_relationships.jsonl"),
            "canonicalization_diagnostics": str(processed_dir / "canonicalization_diagnostics.csv"),
            "industry_expansion": str(processed_dir / "industry_expansion.json"),
            "validation_sample": str(processed_dir / "validation_sample.csv"),
            "dashboard": str(dashboard_path),
            "dashboard_data": str(dashboard_path.parent / "dashboard-data.json"),
        },
    }
