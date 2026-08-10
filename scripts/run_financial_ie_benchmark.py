#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from valuechain.financial_ie.benchmark import BenchmarkRunConfig, BenchmarkRunner
from valuechain.financial_ie.datasets import (
    load_financebench,
    load_finben_fnxl,
    load_finben_ner,
    load_finqa,
    load_fire,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a reproducible financial IE benchmark subset.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="Qwen/Qwen3.6-35B-A3B")
    parser.add_argument(
        "--style",
        choices=["direct", "structured", "retrieval", "workflow"],
        default="structured",
    )
    parser.add_argument("--base-url", default="http://100.114.26.88:31969/v1")
    parser.add_argument("--api-key", default="1969")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-embeddings", action="store_true")
    parser.add_argument("--finben-ner", type=Path)
    parser.add_argument("--finben-fnxl", type=Path)
    parser.add_argument("--fire-data", type=Path)
    parser.add_argument("--fire-types", type=Path)
    parser.add_argument("--finqa", type=Path)
    parser.add_argument("--financebench", type=Path)
    parser.add_argument("--financebench-pdfs", type=Path)
    parser.add_argument("--limit-per-task", type=int, default=30)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict:
    cases = []
    if args.finben_ner:
        cases.extend(load_finben_ner(args.finben_ner, limit=args.limit_per_task))
    if args.finben_fnxl:
        cases.extend(load_finben_fnxl(args.finben_fnxl, limit=min(20, args.limit_per_task)))
    if args.fire_data:
        if not args.fire_types:
            raise ValueError("--fire-types is required with --fire-data")
        cases.extend(load_fire(args.fire_data, args.fire_types, limit=args.limit_per_task))
    if args.finqa:
        cases.extend(load_finqa(args.finqa, limit=args.limit_per_task))
    if args.financebench:
        if not args.financebench_pdfs:
            raise ValueError("--financebench-pdfs is required with --financebench")
        cases.extend(
            load_financebench(
                args.financebench,
                args.financebench_pdfs,
                limit=args.limit_per_task,
            )
        )
    if args.style == "retrieval":
        cases = [case for case in cases if case.task == "financebench"]
    if not cases:
        raise ValueError("No benchmark datasets were configured")
    runner = BenchmarkRunner(
        BenchmarkRunConfig(
            output_dir=args.output_dir,
            model=args.model,
            style=args.style,
            base_url=args.base_url,
            api_key=args.api_key,
            concurrency=args.concurrency,
            use_embeddings=not args.no_embeddings,
        )
    )
    return await runner.run(cases)


def main() -> None:
    args = parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
