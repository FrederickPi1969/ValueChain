from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from valuechain.postgres_acquisition_state import PostgresAcquisitionState
from valuechain.acquisition_worker import run_worker_loop
from valuechain.async_sec_acquisition import AsyncSecAcquisitionRunner
from valuechain.sec_acquisition import AcquisitionConfig


ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="valuechain-acquire")
    subparsers = parser.add_subparsers(dest="command", required=True)

    batch = subparsers.add_parser("run-batch", help="Run one resumable low-concurrency SEC batch.")
    batch.add_argument("--issuer-limit", type=int, default=None)
    subparsers.add_parser("run-worker", help="Continuously drain SEC checkpoints asynchronously.")

    subparsers.add_parser("refresh-universe", help="Refresh the live SEC issuer universe.")
    subparsers.add_parser("status", help="Print acquisition checkpoint statistics.")
    migrate = subparsers.add_parser(
        "migrate-sqlite",
        help="Import the prototype SQLite checkpoint into authoritative Postgres tables.",
    )
    migrate.add_argument("--sqlite-path", default="")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = AcquisitionConfig.from_env()
    if args.command == "status":
        with PostgresAcquisitionState(config.database_url) as state:
            print(json.dumps(state.stats(), indent=2, sort_keys=True))
        return
    if args.command == "migrate-sqlite":
        sqlite_path = Path(args.sqlite_path).expanduser() if args.sqlite_path else config.state_path
        with PostgresAcquisitionState(config.database_url) as state:
            result = state.import_sqlite(sqlite_path, config.target_years)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.command == "run-batch" and args.issuer_limit is not None:
        config = AcquisitionConfig(**{**config.__dict__, "issuer_limit": args.issuer_limit})
    runner = AsyncSecAcquisitionRunner(config, ROOT)
    if args.command == "refresh-universe":
        count = asyncio.run(runner.refresh_universe())
        print(json.dumps({"issuers_upserted": count}, indent=2))
        return
    if args.command == "run-worker":
        asyncio.run(run_worker_loop(runner.run_batch))
        return
    result = asyncio.run(runner.run_batch())
    print(json.dumps(result, indent=2, sort_keys=True))
