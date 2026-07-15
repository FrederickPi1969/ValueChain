#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from valuechain.financial_ie.multilingual.experiment import (
    MultilingualExperiment,
    MultilingualExperimentConfig,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the no-database Chinese/Japanese/Korean financial IE experiment."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--input", action="append", type=Path, default=[])
    parser.add_argument("--input-list", type=Path)
    parser.add_argument("--model", default="Qwen/Qwen3.6-35B-A3B")
    parser.add_argument("--llm-base-url", default="http://100.114.26.88:31969/v1")
    parser.add_argument("--llm-api-key", default=os.getenv("LOCAL_LLM_API_KEY", "1969"))
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 4:
        parser.error("--concurrency must be between 1 and 4")
    if "localllm.frederickpi.com" in args.llm_base_url:
        parser.error("Use the direct Endeavor aggregate endpoint to avoid Cloudflare timeouts")
    return args


def collect_inputs(args: argparse.Namespace) -> tuple[Path, ...]:
    paths = list(args.input)
    if args.input_list:
        for line in args.input_list.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean and not clean.startswith("#"):
                paths.append(Path(clean))
    unique = tuple(dict.fromkeys(paths))
    if not unique:
        raise SystemExit("At least one --input or --input-list entry is required")
    missing = [path for path in unique if not path.exists()]
    if missing:
        raise SystemExit(f"Input files do not exist: {', '.join(map(str, missing[:5]))}")
    return unique


def main() -> None:
    args = parse_args()
    config = MultilingualExperimentConfig(
        output_dir=args.output_dir,
        input_paths=collect_inputs(args),
        model=args.model,
        llm_base_url=args.llm_base_url,
        llm_api_key=args.llm_api_key,
        concurrency=args.concurrency,
    )
    summary = asyncio.run(MultilingualExperiment(config).run())
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
