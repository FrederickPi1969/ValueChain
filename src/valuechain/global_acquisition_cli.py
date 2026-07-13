from __future__ import annotations

import argparse
import asyncio
import json

from valuechain.acquisition_worker import acquisition_process_lock, run_worker_loop
from valuechain.async_global_acquisition import AsyncGlobalAcquisitionRunner
from valuechain.global_acquisition import (
    GLEIF_SOURCE,
    SUPPORTED_SOURCES,
    GlobalAcquisitionConfig,
    run_source,
    source_status,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="valuechain-global-acquire")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run-batch", help="Run one resumable global-source batch.")
    run.add_argument("--source", required=True, choices=SUPPORTED_SOURCES)
    worker = subparsers.add_parser(
        "run-worker", help="Continuously drain one global source asynchronously."
    )
    worker.add_argument(
        "--source", required=True, choices=("cninfo", "priority_eu_esef")
    )
    status = subparsers.add_parser("status", help="Show global-source acquisition statistics.")
    status.add_argument("--source", choices=SUPPORTED_SOURCES, default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = GlobalAcquisitionConfig.from_env()
    if args.command == "run-worker":
        runner = AsyncGlobalAcquisitionRunner(args.source, config)
        with acquisition_process_lock(config.database_url, args.source):
            asyncio.run(run_worker_loop(runner.run_batch))
        return
    if args.command == "run-batch":
        with acquisition_process_lock(config.database_url, args.source):
            if args.source == GLEIF_SOURCE:
                payload = run_source(args.source, config)
            else:
                runner = AsyncGlobalAcquisitionRunner(args.source, config)
                payload = asyncio.run(runner.run_batch())
    else:
        sources = (args.source,) if args.source else SUPPORTED_SOURCES
        payload = {source: source_status(source, config) for source in sources}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str, sort_keys=True))
