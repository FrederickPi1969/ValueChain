"""Copy already-verified official 10-K bundles to Cosmos, resumably."""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import time
from pathlib import Path


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        conn.execute("ALTER TABLE filings ADD COLUMN cosmos_synced_at TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sync_one(row: sqlite3.Row, host: str, root: str) -> None:
    source = Path(row["local_path"]).parent
    if not source.joinpath("metadata.json").is_file() or not Path(row["local_path"]).is_file():
        raise RuntimeError("local 10-K bundle is incomplete")
    remote = f"{root.rstrip('/')}/{row['ticker']}/{row['accession'].replace('-', '')}"
    subprocess.run(["ssh", host, "mkdir", "-p", remote], check=True, timeout=45)
    subprocess.run(["rsync", "-a", "--partial", f"{source}/", f"{host}:{remote}/"], check=True, timeout=300)


def drain(db: Path, host: str, root: str) -> int:
    conn = connect(db)
    rows = conn.execute("""SELECT f.*, c.ticker FROM filings f JOIN companies c ON c.id=f.company_id
                           WHERE f.cosmos_synced_at IS NULL ORDER BY f.id LIMIT 100""").fetchall()
    for row in rows:
        try:
            sync_one(row, host, root)
            conn.execute("UPDATE filings SET cosmos_synced_at=? WHERE id=?", (stamp(), row["id"]))
            print(f"synced {row['ticker']} {row['accession']}", flush=True)
        except Exception as exc:
            print(f"FAILED {row['ticker']} {row['accession']}: {exc}", flush=True)
    conn.close()
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--host", default="pi@100.102.250.107")
    p.add_argument("--root", default="/mnt/hdd8tb/valuechain/annual_filings/2025")
    p.add_argument("--watch-seconds", type=int, default=0)
    args = p.parse_args()
    deadline = time.monotonic() + args.watch_seconds
    while True:
        found = drain(args.db, args.host, args.root)
        if not args.watch_seconds or time.monotonic() >= deadline:
            return
        time.sleep(15 if found else 30)


if __name__ == "__main__":
    main()
