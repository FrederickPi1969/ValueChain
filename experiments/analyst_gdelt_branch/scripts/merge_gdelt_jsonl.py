from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def main() -> int:
    args = parse_args()
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for input_path in [Path(value) for value in args.inputs]:
        for row in read_jsonl(input_path):
            key = dedupe_key(row)
            if key not in merged:
                merged[key] = row
                continue
            merged[key] = merge_row(merged[key], row)
    rows = sorted(
        merged.values(),
        key=lambda row: (str(row.get("ticker", "")), str(row.get("seendate", "")), str(row.get("title", ""))),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(output, rows)
    print(f"Merged {len(rows)} GDELT records into {output}")
    for ticker in sorted({str(row.get("ticker", "")) for row in rows}):
        print(ticker, sum(1 for row in rows if row.get("ticker") == ticker))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge GDELT JSONL outputs and deduplicate per ticker/article.")
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def dedupe_key(row: dict[str, Any]) -> tuple[str, str]:
    ticker = str(row.get("ticker", ""))
    article_id = str(row.get("url") or row.get("canonical_title") or canonical_title(str(row.get("title", ""))))
    return ticker, article_id


def merge_row(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    output = dict(left)
    output["query_modes"] = merge_semicolon(str(left.get("query_modes") or left.get("query_mode", "")), str(right.get("query_modes") or right.get("query_mode", "")))
    output["sec_objects"] = merge_semicolon(str(left.get("sec_objects") or left.get("sec_object", "")), str(right.get("sec_objects") or right.get("sec_object", "")))
    return output


def merge_semicolon(left: str, right: str) -> str:
    values = [item for item in left.split(";") if item] + [item for item in right.split(";") if item]
    return ";".join(sorted(set(values)))


def canonical_title(title: str) -> str:
    lowered = title.lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
