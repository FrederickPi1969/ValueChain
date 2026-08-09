"""Resumable official-SEC 10-K collector.

This intentionally does not use search-engine quota.  It resolves each ticker
against SEC's official ticker map, selects the most recent 10-K whose report
date falls in the requested calendar year, and downloads the SEC primary
document together with clean provenance metadata.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

SEC_DATA = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"
USER_AGENT = "ValueChain annual-filings research contact=frederickpi@example.com"


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SharedRateLimit:
    """A process-wide SEC throttle; stays safely below SEC's 10 req/sec cap."""
    def __init__(self, rps: float) -> None:
        self.delay = 1 / rps
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0, self.next_at - now)
            self.next_at = max(self.next_at, now) + self.delay
        if delay:
            time.sleep(delay)


RATE = SharedRateLimit(7.0)
SESSION = threading.local()


def http() -> requests.Session:
    if not hasattr(SESSION, "client"):
        client = requests.Session()
        client.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        SESSION.client = client
    return SESSION.client


def get(url: str, *, binary: bool = False) -> Any:
    last: Exception | None = None
    for attempt in range(4):
        try:
            RATE.wait()
            response = http().get(url, timeout=45, headers={"Accept": "*/*" if binary else "application/json"})
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response.content if binary else response.json()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"SEC request failed: {last}")


def normalized(ticker: str) -> str:
    return ticker.upper().replace(".", "-").replace("/", "-").strip()


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def init(db: Path, csvs: list[Path]) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, company_name TEXT,
        cik TEXT, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
        updated_at TEXT
      );
      CREATE TABLE IF NOT EXISTS filings (
        id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, form TEXT NOT NULL,
        accession TEXT NOT NULL, report_date TEXT, filing_date TEXT,
        primary_document TEXT, source_url TEXT NOT NULL, local_path TEXT,
        sha256 TEXT, byte_count INTEGER, selection_reason TEXT, downloaded_at TEXT,
        UNIQUE(company_id, accession)
      );
      CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, attempted_at TEXT NOT NULL,
        outcome TEXT NOT NULL, detail TEXT
      );
    """)
    for csv_path in csvs:
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                ticker = (row.get("Ticker/Abbreviation") or row.get("ticker") or "").strip().upper()
                name = (row.get("Full Name") or row.get("company_name") or "").strip()
                if ticker:
                    conn.execute("INSERT OR IGNORE INTO companies(ticker, company_name, updated_at) VALUES (?, ?, ?)", (ticker, name, stamp()))
    conn.close()


def ticker_map() -> dict[str, dict[str, str]]:
    payload = get(f"{SEC_WWW}/files/company_tickers_exchange.json")
    fields = payload["fields"]
    result = {}
    for values in payload["data"]:
        row = dict(zip(fields, values))
        ticker = normalized(str(row.get("ticker", "")))
        if ticker:
            result[ticker] = {"cik": str(row["cik"]).zfill(10), "name": str(row.get("name", ""))}
    return result


def choose_10k(payload: dict[str, Any], year: int) -> tuple[dict[str, str], str] | None:
    recent = payload.get("filings", {}).get("recent", {})
    rows = []
    for i, form in enumerate(recent.get("form", [])):
        if form != "10-K":
            continue
        row = {key: str(values[i] or "") if i < len(values) else "" for key, values in recent.items() if isinstance(values, list)}
        rows.append(row)
    in_year = [r for r in rows if r.get("reportDate", "").startswith(str(year))]
    if in_year:
        return max(in_year, key=lambda r: (r.get("reportDate", ""), r.get("filingDate", ""))), "report_date_in_calendar_year"
    # Fiscal year ends late in the prior year but 10-Ks are often filed in the
    # following Q1. Keep this fallback explicit in the provenance record.
    filing_window = [r for r in rows if f"{year}-10-01" <= r.get("filingDate", "") <= f"{year + 1}-04-30"]
    if filing_window:
        return max(filing_window, key=lambda r: r.get("filingDate", "")), "q4_to_next_q1_filing_date_fallback"
    return None


def claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM companies WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE companies SET status='running', updated_at=? WHERE id=?", (stamp(), row["id"]))
    conn.execute("COMMIT")
    return row


def process(db: Path, lookup: dict[str, dict[str, str]], output: Path, year: int) -> None:
    conn = connect(db)
    while (company := claim(conn)) is not None:
        try:
            details = lookup.get(normalized(company["ticker"]))
            if not details:
                conn.execute("UPDATE companies SET status='not_sec_issuer', error=NULL, updated_at=? WHERE id=?", (stamp(), company["id"]))
                conn.execute("INSERT INTO attempts(company_id,attempted_at,outcome,detail) VALUES(?,?,?,?)", (company["id"], stamp(), "not_sec_issuer", "ticker missing from SEC ticker map"))
                continue
            cik = details["cik"]
            conn.execute("UPDATE companies SET cik=?, updated_at=? WHERE id=?", (cik, stamp(), company["id"]))
            selected = choose_10k(get(f"{SEC_DATA}/submissions/CIK{cik}.json"), year)
            if not selected:
                conn.execute("UPDATE companies SET status='no_matching_10k', error=NULL, updated_at=? WHERE id=?", (stamp(), company["id"]))
                conn.execute("INSERT INTO attempts(company_id,attempted_at,outcome,detail) VALUES(?,?,?,?)", (company["id"], stamp(), "no_matching_10k", str(year)))
                continue
            filing, reason = selected
            accession = filing["accessionNumber"]
            accession_clean = accession.replace("-", "")
            primary = filing.get("primaryDocument") or ""
            if not primary:
                raise RuntimeError(f"missing primary document for {accession}")
            url = f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{accession_clean}/{primary}"
            content = get(url, binary=True)
            target = output / company["ticker"] / accession_clean
            target.mkdir(parents=True, exist_ok=True)
            path = target / primary
            path.write_bytes(content)
            record = {
                "ticker": company["ticker"], "company_name": company["company_name"] or details["name"], "cik": cik,
                "form": "10-K", "accession": accession, "report_date": filing.get("reportDate"),
                "filing_date": filing.get("filingDate"), "primary_document": primary, "source_url": url,
                "selection_reason": reason, "sha256": hashlib.sha256(content).hexdigest(), "byte_count": len(content),
                "downloaded_at": stamp(),
            }
            (target / "metadata.json").write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
            conn.execute("""INSERT OR REPLACE INTO filings(company_id,form,accession,report_date,filing_date,primary_document,source_url,local_path,sha256,byte_count,selection_reason,downloaded_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (company["id"], "10-K", accession, record["report_date"], record["filing_date"], primary, url, str(path), record["sha256"], len(content), reason, record["downloaded_at"]))
            conn.execute("UPDATE companies SET status='downloaded', error=NULL, updated_at=? WHERE id=?", (stamp(), company["id"]))
            conn.execute("INSERT INTO attempts(company_id,attempted_at,outcome,detail) VALUES(?,?,?,?)", (company["id"], stamp(), "downloaded", url))
            print(f"{company['ticker']} 10-K {filing.get('reportDate')} downloaded", flush=True)
        except Exception as exc:
            conn.execute("UPDATE companies SET status='failed', error=?, updated_at=? WHERE id=?", (f"{type(exc).__name__}: {exc}", stamp(), company["id"]))
            conn.execute("INSERT INTO attempts(company_id,attempted_at,outcome,detail) VALUES(?,?,?,?)", (company["id"], stamp(), "failed", f"{type(exc).__name__}: {exc}"))
            print(f"FAILED {company['ticker']}: {exc}", flush=True)
    conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path, action="append", required=True, help="May be passed more than once")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--retry-failed", action="store_true")
    args = p.parse_args()
    init(args.db, args.csv)
    if args.retry_failed:
        conn = connect(args.db); conn.execute("UPDATE companies SET status='pending', error=NULL WHERE status='failed'"); conn.close()
    lookup = ticker_map()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, args.db, lookup, args.output, args.year) for _ in range(args.workers)]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()
