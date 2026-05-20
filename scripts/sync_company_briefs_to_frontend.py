from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from valuechain.config import Settings  # noqa: E402
from valuechain.run_registry import sync_frontend_public_briefs  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync generated company dependency briefs into Vite public data.")
    parser.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="Run id to sync. Repeat for multiple runs. Defaults to every reports/runs/*/briefs directory.",
    )
    return parser.parse_args()


def discover_run_ids(settings: Settings) -> list[str]:
    runs_dir = settings.reports_dir / "runs"
    if not runs_dir.exists():
        return []
    return sorted(path.parent.name for path in runs_dir.glob("*/briefs") if path.is_dir())


def main() -> None:
    args = parse_args()
    settings = Settings()
    run_ids = args.run_id or discover_run_ids(settings)
    if not run_ids:
        print("No brief directories found under reports/runs/*/briefs.")
        return
    for run_id in run_ids:
        rows = sync_frontend_public_briefs(settings, run_id)
        print(f"run_id={run_id} briefs={len(rows)}")


if __name__ == "__main__":
    main()
