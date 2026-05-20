from __future__ import annotations

import argparse
import json
from pathlib import Path

from valuechain.config import Settings, ensure_dirs
from valuechain.gleif import GLEIFClient, run_gleif_resolution
from valuechain.io_utils import write_json
from valuechain.pipeline import PipelineOptions, run_pipeline
from valuechain.planning import build_execution_plan
from valuechain.universe import parse_csv_arg, parse_tickers, read_universe, summarize_universe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valuechain",
        description="Prototype SEC filing to dependency evidence pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    universe = sub.add_parser("universe", help="Inspect the configured company universe before running.")
    add_input_args(universe)
    universe.add_argument("--json", action="store_true", help="Print full universe metadata as JSON.")

    plan = sub.add_parser("plan", help="Build an execution plan without SEC network downloads.")
    add_input_args(plan)
    add_run_shape_args(plan)
    plan.add_argument("--write", action="store_true", help="Write data/processed/input_plan.json.")

    run = sub.add_parser("run", help="Run the end-to-end prototype pipeline.")
    add_input_args(run)
    add_run_shape_args(run)
    run.add_argument("--extractor", choices=["rules", "llm", "hybrid"], default="rules")
    run.add_argument("--min-relevance-score", type=float, default=2.0)
    run.add_argument("--skip-yahoo", action="store_true")
    run.add_argument("--run-id", default="", help="Stable id for this run. Defaults to a timestamped id.")
    run.add_argument("--run-label", default="", help="Human-readable label shown in the frontend run index.")
    run.add_argument("--write-postgres", action="store_true", help="Write run artifacts into Postgres.")
    run.add_argument("--postgres-url", default="", help="Override VALUECHAIN_DATABASE_URL for this run.")
    run.add_argument("--llm-concurrency", type=int, default=None, help="Concurrent LLM extraction requests.")
    run.add_argument("--embedding-merge", action="store_true", help="Use local embedding model for object alias merge.")
    run.add_argument("--embedding-threshold", type=float, default=0.92, help="Cosine threshold for embedding object merge.")
    run.add_argument(
        "--no-exhibits",
        action="store_true",
        help="Disable archive exhibit retrieval and parse only primary filing documents.",
    )
    run.add_argument(
        "--exhibit-types",
        default="EX-10,EX-21,EX-99,EX-99.1",
        help="Comma-separated exhibit type prefixes to include from SEC archive detail pages.",
    )
    run.add_argument(
        "--max-exhibits-per-filing",
        type=int,
        default=8,
        help="Maximum selected exhibit source documents per filing.",
    )

    resolve = sub.add_parser(
        "resolve-entities",
        help="Create a GLEIF-backed resolver candidate queue without modifying graph edges.",
    )
    resolve.add_argument("--run-id", default="", help="Run id whose relation_evidence.jsonl objects should be resolved.")
    resolve.add_argument("--input", default="", help="Explicit relation_evidence JSONL path. Overrides --run-id.")
    resolve.add_argument("--objects", default="", help="Comma-separated object strings to resolve without reading a run.")
    resolve.add_argument("--output-dir", default="", help="Output directory. Defaults to the selected run directory.")
    resolve.add_argument("--output-prefix", default="entity_resolution_candidates")
    resolve.add_argument("--limit-objects", type=int, default=100, help="Max unique objects to send to GLEIF.")
    resolve.add_argument("--min-evidence-count", type=int, default=2)
    resolve.add_argument("--max-candidates", type=int, default=5)
    resolve.add_argument("--gleif-rps", type=float, default=None, help="GLEIF API requests per second.")
    resolve.add_argument("--include-class-objects", action="store_true", help="Also send generic class objects to GLEIF.")
    resolve.add_argument("--include-relationships", action="store_true", help="Fetch available parent relationship records.")
    return parser


def add_input_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--universe", default="data/universe/ai_infra_universe.csv")
    parser.add_argument("--tickers", help="Comma-separated ticker subset, e.g. NVDA,AMD,MSFT.")
    parser.add_argument("--roles", help="Comma-separated role subset, e.g. foundry,cloud_hyperscaler.")
    parser.add_argument("--priority", type=int, help="Include companies with priority <= this value.")
    parser.add_argument("--limit-companies", type=int, help="Cap company count after filtering.")


def add_run_shape_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--forms", default="10-K,10-Q,8-K,20-F,6-K")
    parser.add_argument("--max-filings-per-company", type=int, default=2)
    parser.add_argument(
        "--filing-selection",
        choices=["form-balanced", "latest"],
        default="form-balanced",
        help="form-balanced takes up to max filings per selected form; latest preserves the old total latest-filings cap.",
    )
    parser.add_argument("--filing-date-from", default="", help="Inclusive YYYY-MM-DD filing date lower bound.")
    parser.add_argument("--filing-date-to", default="", help="Inclusive YYYY-MM-DD filing date upper bound.")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "universe":
        companies = read_filtered_universe(args)
        summary = summarize_universe(companies)
        if args.json:
            print(json.dumps({"summary": summary, "companies": [c.to_dict() for c in companies]}, indent=2))
            return
        print(f"companies={summary['company_count']}")
        print(f"roles={json.dumps(summary['role_counts'], sort_keys=True)}")
        print(f"priorities={json.dumps(summary['priority_counts'], sort_keys=True)}")
        print("tickers=" + ",".join(summary["tickers"]))
        return
    if args.command == "plan":
        companies = read_filtered_universe(args)
        plan = build_execution_plan(
            companies=companies,
            forms=parse_forms(args.forms),
            max_filings_per_company=args.max_filings_per_company,
            filing_selection=args.filing_selection.replace("-", "_"),
            filing_date_from=args.filing_date_from,
            filing_date_to=args.filing_date_to,
        )
        payload = plan.to_dict()
        if args.write:
            settings = Settings()
            ensure_dirs(settings)
            write_json(settings.processed_dir / "input_plan.json", payload)
        print(json.dumps(payload, indent=2))
        return
    if args.command == "run":
        settings = Settings()
        options = PipelineOptions(
            universe_path=Path(args.universe),
            tickers=parse_tickers(args.tickers),
            roles=parse_csv_arg(args.roles),
            max_priority=args.priority,
            limit_companies=args.limit_companies,
            forms=parse_forms(args.forms),
            max_filings_per_company=args.max_filings_per_company,
            filing_selection=args.filing_selection.replace("-", "_"),
            filing_date_from=args.filing_date_from,
            filing_date_to=args.filing_date_to,
            extractor=args.extractor,
            min_relevance_score=args.min_relevance_score,
            skip_yahoo=args.skip_yahoo,
            run_id=args.run_id,
            run_label=args.run_label,
            write_postgres=args.write_postgres,
            postgres_url=args.postgres_url,
            llm_concurrency=args.llm_concurrency or settings.llm_concurrency,
            embedding_merge=args.embedding_merge,
            embedding_threshold=args.embedding_threshold,
            include_exhibits=not args.no_exhibits,
            exhibit_types=parse_forms(args.exhibit_types),
            max_exhibits_per_filing=args.max_exhibits_per_filing,
        )
        result = run_pipeline(settings, options)
        print(f"run_id={result.run_id}")
        print(f"companies={len(result.companies)}")
        print(f"filings={len(result.filings)}")
        print(f"source_documents={len(result.source_documents)}")
        print(f"exhibit_documents={sum(1 for document in result.source_documents if not document.is_primary)}")
        print(f"passages={len(result.passages)}")
        print(f"candidate_passages={len(result.candidate_passages)}")
        print(f"relation_evidence={len(result.evidence)}")
        print(f"graph_edges={len(result.edges)}")
        print(f"dashboard={result.dashboard_path}")
        print(f"frontend_index={result.index_path}")
        return
    if args.command == "resolve-entities":
        settings = Settings()
        ensure_dirs(settings)
        objects = parse_csv_arg(args.objects)
        evidence_path = Path(args.input) if args.input else None
        if not evidence_path and args.run_id:
            evidence_path = settings.processed_dir / "runs" / args.run_id / "relation_evidence.jsonl"
        if not objects and (not evidence_path or not evidence_path.exists()):
            parser.error("--run-id, --input, or --objects is required; relation_evidence.jsonl must exist for run/input mode.")
        output_dir = Path(args.output_dir) if args.output_dir else (
            evidence_path.parent if evidence_path else settings.processed_dir / "entity_resolution"
        )
        client = GLEIFClient(
            requests_per_second=args.gleif_rps if args.gleif_rps is not None else settings.gleif_rps,
            proxies=settings.proxies,
        )
        result = run_gleif_resolution(
            evidence_path=evidence_path,
            objects=objects,
            output_dir=output_dir,
            client=client,
            limit_objects=args.limit_objects,
            min_evidence_count=args.min_evidence_count,
            max_candidates=args.max_candidates,
            include_class_objects=args.include_class_objects,
            include_relationships=args.include_relationships,
            output_prefix=args.output_prefix,
        )
        print(f"objects={len(result['contexts'])}")
        print(f"candidate_rows={len(result['candidates'])}")
        for name, path in result["paths"].items():
            print(f"{name}={path}")


def read_filtered_universe(args: argparse.Namespace):
    return read_universe(
        Path(args.universe),
        tickers=parse_tickers(args.tickers),
        roles=parse_csv_arg(args.roles),
        max_priority=args.priority,
        limit=args.limit_companies,
    )


def parse_forms(forms: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in forms.split(",") if part.strip())


if __name__ == "__main__":
    main()
