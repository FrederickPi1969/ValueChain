from __future__ import annotations

import argparse
import json
from pathlib import Path

from valuechain.acquisition_state import AcquisitionState
from valuechain.sec_acquisition import AcquisitionConfig, SecAcquisitionRunner


ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="valuechain-acquire")
    subparsers = parser.add_subparsers(dest="command", required=True)

    batch = subparsers.add_parser("run-batch", help="Run one resumable low-concurrency SEC batch.")
    batch.add_argument("--issuer-limit", type=int, default=None)

    subparsers.add_parser("refresh-universe", help="Refresh the live SEC issuer universe.")
    subparsers.add_parser("status", help="Print acquisition checkpoint statistics.")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = AcquisitionConfig.from_env()
    if args.command == "status":
        with AcquisitionState(config.state_path) as state:
            print(json.dumps(state.stats(), indent=2, sort_keys=True))
        return
    if args.command == "run-batch" and args.issuer_limit is not None:
        config = AcquisitionConfig(**{**config.__dict__, "issuer_limit": args.issuer_limit})
    runner = SecAcquisitionRunner(config, ROOT)
    if args.command == "refresh-universe":
        with AcquisitionState(config.state_path) as state:
            count = runner.refresh_universe(state)
        print(json.dumps({"issuers_upserted": count}, indent=2))
        return
    result = runner.run_batch()
    print(json.dumps(result, indent=2, sort_keys=True))
