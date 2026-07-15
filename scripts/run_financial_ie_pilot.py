#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from valuechain.financial_ie.pilot import FinancialIEPilot, PilotRunConfig
from valuechain.financial_ie.pilot_sources import CatalogConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the no-database 100-company financial IE audit pilot.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--universe", type=Path, default=Path("data/universe/ai_infra_universe.csv"))
    parser.add_argument("--target-count", type=int, default=100)
    parser.add_argument("--model", default="Qwen/Qwen3.6-35B-A3B")
    parser.add_argument("--llm-base-url", default="http://100.114.26.88:31969/v1")
    parser.add_argument("--llm-api-key", default="1969")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--catalog-base-url", default="http://127.0.0.1:18018")
    parser.add_argument("--catalog-token", default=os.getenv("VALUECHAIN_FILE_API_TOKEN", ""))
    parser.add_argument("--filing-root", type=Path, default=Path("/mnt/hdd8tb/filings/sec_edgar"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = PilotRunConfig(
        output_dir=args.output_dir,
        universe_path=args.universe,
        target_count=args.target_count,
        model=args.model,
        llm_base_url=args.llm_base_url,
        llm_api_key=args.llm_api_key,
        concurrency=args.concurrency,
        catalog=CatalogConfig(
            base_url=args.catalog_base_url,
            token=args.catalog_token,
            filing_root=args.filing_root,
        ),
    )
    print(json.dumps(asyncio.run(FinancialIEPilot(config).run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
