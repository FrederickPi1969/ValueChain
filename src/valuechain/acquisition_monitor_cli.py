from __future__ import annotations

import argparse
import json

from valuechain.acquisition_monitor import AcquisitionHealthMonitor, MonitorConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="valuechain-monitor")
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Always return zero after writing the health report.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    report = AcquisitionHealthMonitor(MonitorConfig.from_env()).run()
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if report.overall_status == "critical" and not args.no_fail:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

