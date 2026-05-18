from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from valuechain.aggregation import aggregate_edges, bottleneck_candidates
from valuechain.config import Settings, ensure_dirs
from valuechain.dashboard import render_dashboard
from valuechain.entity_resolution import EntityResolver
from valuechain.filing_parser import parse_sections, segment_passages
from valuechain.io_utils import write_csv, write_jsonl
from valuechain.llm_client import LLMConfig, OpenAICompatibleClient
from valuechain.models import Company, FilingRecord, GraphEdge, Passage, RelationEvidence
from valuechain.relation_llm import HybridRelationExtractor, LLMRelationExtractor
from valuechain.relation_rules import RuleBasedRelationExtractor
from valuechain.relevance import filter_candidates
from valuechain.sec_client import SECClient
from valuechain.universe import read_universe
from valuechain.yahoo_enrichment import fetch_yahoo_snapshot


@dataclass(frozen=True)
class PipelineOptions:
    universe_path: Path
    tickers: list[str] | None = None
    forms: tuple[str, ...] = ("10-K", "10-Q", "8-K", "20-F", "6-K")
    max_filings_per_company: int = 2
    extractor: str = "rules"
    min_relevance_score: float = 2.0
    skip_yahoo: bool = False


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


def run_pipeline(settings: Settings, options: PipelineOptions) -> PipelineResult:
    ensure_dirs(settings)
    companies = read_universe(options.universe_path, options.tickers)
    sec_client = SECClient(
        user_agent=settings.sec_user_agent,
        requests_per_second=settings.sec_rps,
        proxies=settings.proxies,
    )
    resolved_companies = sec_client.resolve_companies(companies)
    write_csv(
        settings.processed_dir / "company_universe_resolved.csv",
        [company.to_dict() for company in resolved_companies],
        fieldnames=["ticker", "company_name", "role", "priority", "notes", "cik", "exchange"],
    )

    filings = discover_and_download_filings(sec_client, resolved_companies, settings, options)
    write_csv(settings.processed_dir / "filing_manifest.csv", [filing.to_dict() for filing in filings])

    passages = parse_all_passages(filings)
    candidate_passages = filter_candidates(passages, min_score=options.min_relevance_score)
    write_jsonl(settings.processed_dir / "passages.jsonl", [passage.to_dict() for passage in passages])
    write_jsonl(
        settings.processed_dir / "candidate_passages.jsonl",
        [passage.to_dict() for passage in candidate_passages],
    )

    extractor = build_extractor(settings, options, resolved_companies)
    evidence = extract_relations(candidate_passages, extractor)
    write_jsonl(settings.processed_dir / "relation_evidence.jsonl", [record.to_dict() for record in evidence])

    edges = aggregate_edges(evidence)
    write_csv(settings.processed_dir / "graph_edges.csv", [edge.to_dict() for edge in edges])
    write_csv(settings.processed_dir / "bottleneck_candidates.csv", bottleneck_candidates(edges))

    yahoo_rows = [] if options.skip_yahoo else fetch_yahoo_snapshot(resolved_companies)
    if yahoo_rows:
        write_csv(settings.processed_dir / "yahoo_snapshot.csv", yahoo_rows)

    dashboard_path = settings.reports_dir / "dashboard.html"
    render_dashboard(dashboard_path, edges, evidence, yahoo_rows)

    return PipelineResult(
        companies=resolved_companies,
        filings=filings,
        passages=passages,
        candidate_passages=candidate_passages,
        evidence=evidence,
        edges=edges,
        yahoo_rows=yahoo_rows,
        dashboard_path=dashboard_path,
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
        )
    )
    llm = LLMRelationExtractor(llm_client, model_version=settings.extraction_model)
    if options.extractor == "llm":
        return llm
    if options.extractor == "hybrid":
        return HybridRelationExtractor(rules, llm)
    raise ValueError(f"Unknown extractor: {options.extractor}")


def extract_relations(candidate_passages: list[Passage], extractor) -> list[RelationEvidence]:
    records: list[RelationEvidence] = []
    for passage in candidate_passages:
        records.extend(extractor.extract(passage))
    return records

