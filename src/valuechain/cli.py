from __future__ import annotations

import argparse
import json
from pathlib import Path

from valuechain.config import Settings, ensure_dirs
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
        )
        result = run_pipeline(settings, options)
        print(f"run_id={result.run_id}")
        print(f"companies={len(result.companies)}")
        print(f"filings={len(result.filings)}")
        print(f"passages={len(result.passages)}")
        print(f"candidate_passages={len(result.candidate_passages)}")
        print(f"relation_evidence={len(result.evidence)}")
        print(f"graph_edges={len(result.edges)}")
        print(f"dashboard={result.dashboard_path}")
        print(f"frontend_index={result.index_path}")


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
