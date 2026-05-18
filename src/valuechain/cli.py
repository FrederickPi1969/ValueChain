from __future__ import annotations

import argparse
from pathlib import Path

from valuechain.config import Settings
from valuechain.pipeline import PipelineOptions, run_pipeline
from valuechain.universe import parse_tickers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valuechain",
        description="Prototype SEC filing to dependency evidence pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the end-to-end prototype pipeline.")
    run.add_argument("--universe", default="data/universe/ai_infra_universe.csv")
    run.add_argument("--tickers", help="Comma-separated ticker subset, e.g. NVDA,AMD,MSFT.")
    run.add_argument("--forms", default="10-K,10-Q,8-K,20-F,6-K")
    run.add_argument("--max-filings-per-company", type=int, default=2)
    run.add_argument("--extractor", choices=["rules", "llm", "hybrid"], default="rules")
    run.add_argument("--min-relevance-score", type=float, default=2.0)
    run.add_argument("--skip-yahoo", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        settings = Settings()
        options = PipelineOptions(
            universe_path=Path(args.universe),
            tickers=parse_tickers(args.tickers),
            forms=tuple(part.strip() for part in args.forms.split(",") if part.strip()),
            max_filings_per_company=args.max_filings_per_company,
            extractor=args.extractor,
            min_relevance_score=args.min_relevance_score,
            skip_yahoo=args.skip_yahoo,
        )
        result = run_pipeline(settings, options)
        print(f"companies={len(result.companies)}")
        print(f"filings={len(result.filings)}")
        print(f"passages={len(result.passages)}")
        print(f"candidate_passages={len(result.candidate_passages)}")
        print(f"relation_evidence={len(result.evidence)}")
        print(f"graph_edges={len(result.edges)}")
        print(f"dashboard={result.dashboard_path}")


if __name__ == "__main__":
    main()

