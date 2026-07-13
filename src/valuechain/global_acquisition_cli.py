from __future__ import annotations

import argparse
import json

from valuechain.global_acquisition import (
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
    status = subparsers.add_parser("status", help="Show global-source acquisition statistics.")
    status.add_argument("--source", choices=SUPPORTED_SOURCES, default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = GlobalAcquisitionConfig.from_env()
    if args.command == "run-batch":
        payload = run_source(args.source, config)
    else:
        sources = (args.source,) if args.source else SUPPORTED_SOURCES
        payload = {source: source_status(source, config) for source in sources}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str, sort_keys=True))
