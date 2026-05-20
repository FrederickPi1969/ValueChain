#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from valuechain.company_dependency_brief import (  # noqa: E402
    BriefLLMConfig,
    BriefOptions,
    BriefReportLLMClient,
    generate_company_dependency_brief,
    write_company_dependency_brief,
)
from valuechain.config import Settings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a company dependency brief from existing run artifacts.")
    parser.add_argument("--company", required=True, help="Ticker, CIK, or company name, e.g. NVDA.")
    parser.add_argument("--run-id", default="", help="Run id under data/processed/runs.")
    parser.add_argument("--run-dir", default="", help="Explicit processed run directory.")
    parser.add_argument("--output-dir", default="", help="Directory for generated Markdown/JSON brief.")
    parser.add_argument("--model", default="", help="Report generation model. Defaults to VALUECHAIN_COMPLEX_MODEL.")
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL. Defaults to VALUECHAIN_LLM_BASE_URL.")
    parser.add_argument("--api-key", default="", help="Bearer token. Defaults to VALUECHAIN_LLM_API_KEY.")
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic analyst interpretation fallback.")
    parser.add_argument("--max-claims", type=int, default=8, help="Max claims per brief section.")
    parser.add_argument("--max-evidence-rows", type=int, default=28, help="Max evidence table rows.")
    parser.add_argument("--min-current-confidence", type=float, default=0.72, help="Current-fact edge confidence floor.")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = Settings()
    run_dir = resolve_run_dir(settings, args)
    output_dir = resolve_output_dir(settings, args, run_dir)
    model = args.model or settings.complex_model
    llm_client = None
    if not args.no_llm:
        llm_client = BriefReportLLMClient(
            BriefLLMConfig(
                base_url=args.base_url or settings.llm_base_url,
                api_key=args.api_key or settings.llm_api_key,
                model=model,
                proxy_url=settings.https_proxy or settings.http_proxy,
                timeout_s=180,
            )
        )
    brief = generate_company_dependency_brief(
        run_dir=run_dir,
        company_query=args.company,
        llm_client=llm_client,
        model_version=model if llm_client else "deterministic",
        options=BriefOptions(
            max_claims_per_section=args.max_claims,
            max_evidence_table_rows=args.max_evidence_rows,
            min_current_fact_confidence=args.min_current_confidence,
        ),
    )
    paths = write_company_dependency_brief(brief, output_dir)
    print(f"company={brief.company.get('company_name')}")
    print(f"ticker={brief.company.get('ticker')}")
    print(f"run_dir={run_dir}")
    for name, path in paths.items():
        print(f"{name}={path}")


def resolve_run_dir(settings: Settings, args: argparse.Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser().resolve()
    if args.run_id:
        return settings.processed_dir / "runs" / args.run_id
    return settings.processed_dir


def resolve_output_dir(settings: Settings, args: argparse.Namespace, run_dir: Path) -> Path:
    if args.output_dir:
        return Path(args.output_dir).expanduser().resolve()
    if run_dir.parent.name == "runs":
        return settings.reports_dir / "runs" / run_dir.name / "briefs"
    return settings.reports_dir / "briefs"


if __name__ == "__main__":
    main()
