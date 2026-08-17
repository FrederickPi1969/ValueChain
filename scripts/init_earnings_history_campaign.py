"""Initialize a resumable historical earnings-call campaign database.

This command is deliberately acquisition-free: it reads audited local source
databases, writes campaign/company/job metadata, and never performs a search or
download.  Company identity is anchored on SEC CIK and the default universe is
limited to issuers whose downloaded 10-K reports a United States state of
incorporation in inline XBRL.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_SEED = "valuechain-us-earnings-history-pilot-v1"
DEFAULT_PILOT_SIZE = 100
VALID_QUARTERS = ("Q1", "Q2", "Q3", "Q4")

US_STATE_NAME_TO_CODE = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
US_STATE_CODES = frozenset(US_STATE_NAME_TO_CODE.values())
UNITED_STATES_LABELS = frozenset(
    {"united states", "united states of america", "us", "u.s.", "usa"}
)

INCORPORATION_FACT = re.compile(
    rb"name\s*=\s*[\"']dei:EntityIncorporationStateCountryCode[\"']"
    rb"[^>]*>(?P<body>.{0,16384}?)</ix:nonNumeric\s*>",
    re.IGNORECASE | re.DOTALL,
)
HTML_TAG = re.compile(rb"<[^>]+>")
WHITESPACE = re.compile(r"\s+")


SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA user_version=1;

CREATE TABLE IF NOT EXISTS campaign (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  schema_version INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('pilot', 'full')),
  seed TEXT NOT NULL,
  pilot_size INTEGER NOT NULL CHECK (pilot_size > 0),
  annual_db TEXT NOT NULL,
  top_db TEXT NOT NULL,
  next_db TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
  cik TEXT PRIMARY KEY,
  ticker TEXT NOT NULL UNIQUE,
  company_name TEXT NOT NULL,
  sec_company_name TEXT NOT NULL,
  sector TEXT NOT NULL,
  sector_group TEXT NOT NULL,
  cohort TEXT NOT NULL CHECK (cohort IN ('top', 'next')),
  tier INTEGER NOT NULL CHECK (tier IN (1, 2)),
  source_company_id INTEGER NOT NULL,
  source_db TEXT NOT NULL,
  incorporation_code TEXT NOT NULL,
  incorporation_normalized TEXT NOT NULL,
  company_priority INTEGER NOT NULL UNIQUE CHECK (company_priority > 0),
  pilot_selected INTEGER NOT NULL DEFAULT 0 CHECK (pilot_selected IN (0, 1)),
  pilot_stratum TEXT,
  pilot_rank INTEGER UNIQUE,
  sample_score TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (
    (pilot_selected = 1 AND pilot_stratum IS NOT NULL AND pilot_rank IS NOT NULL
      AND sample_score IS NOT NULL)
    OR
    (pilot_selected = 0 AND pilot_stratum IS NULL AND pilot_rank IS NULL
      AND sample_score IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  cik TEXT NOT NULL REFERENCES companies(cik),
  calendar_year INTEGER NOT NULL CHECK (calendar_year BETWEEN 1900 AND 2200),
  quarter TEXT NOT NULL CHECK (quarter IN ('Q1', 'Q2', 'Q3', 'Q4')),
  calendar_target TEXT NOT NULL,
  fiscal_year INTEGER,
  fiscal_quarter TEXT CHECK (
    fiscal_quarter IS NULL OR fiscal_quarter IN ('Q1', 'Q2', 'Q3', 'Q4')
  ),
  period_end TEXT,
  call_date TEXT,
  reported_period_label TEXT,
  period_rank INTEGER NOT NULL CHECK (period_rank >= 0),
  priority INTEGER NOT NULL UNIQUE CHECK (priority > 0),
  stage TEXT NOT NULL DEFAULT 'search' CHECK (
    stage IN ('search', 'download', 'validate', 'publish', 'done')
  ),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN (
      'pending', 'running', 'retry_wait', 'completed', 'exhausted',
      'failed', 'skipped'
    )
  ),
  query_count INTEGER NOT NULL DEFAULT 0 CHECK (query_count BETWEEN 0 AND 4),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  next_attempt_at TEXT,
  lease_owner TEXT,
  lease_token TEXT,
  lease_acquired_at TEXT,
  lease_expires_at TEXT,
  heartbeat_at TEXT,
  last_started_at TEXT,
  last_finished_at TEXT,
  last_error TEXT,
  error_class TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (cik, calendar_year, quarter)
);

CREATE TABLE IF NOT EXISTS search_attempts (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 4),
  engine TEXT NOT NULL CHECK (engine IN ('duckduckgo', 'google')),
  query TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN ('pending', 'running', 'completed', 'failed')
  ),
  result_count INTEGER NOT NULL DEFAULT 0 CHECK (result_count >= 0),
  request_id TEXT,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  UNIQUE (job_id, ordinal)
);

CREATE TABLE IF NOT EXISTS job_attempts (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
  worker_id TEXT NOT NULL,
  lease_token TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (
    stage IN ('search', 'download', 'validate', 'publish')
  ),
  status TEXT NOT NULL CHECK (
    status IN ('started', 'succeeded', 'failed', 'expired', 'cancelled')
  ),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT,
  details_json TEXT,
  UNIQUE (job_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS initialization_runs (
  id INTEGER PRIMARY KEY,
  requested_mode TEXT NOT NULL CHECK (requested_mode IN ('pilot', 'full')),
  effective_mode TEXT NOT NULL CHECK (effective_mode IN ('pilot', 'full')),
  seed TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  strict_us_companies INTEGER NOT NULL,
  pilot_selected INTEGER NOT NULL,
  periods INTEGER NOT NULL,
  jobs_inserted INTEGER NOT NULL,
  jobs_total INTEGER NOT NULL,
  statistics_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_companies_cohort_sector
  ON companies(cohort, sector_group, company_priority);
CREATE INDEX IF NOT EXISTS ix_jobs_claim
  ON jobs(status, next_attempt_at, priority, id);
CREATE INDEX IF NOT EXISTS ix_jobs_lease
  ON jobs(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_jobs_period
  ON jobs(calendar_year, quarter, status);
CREATE INDEX IF NOT EXISTS ix_search_attempts_job
  ON search_attempts(job_id, ordinal);
CREATE INDEX IF NOT EXISTS ix_job_attempts_job
  ON job_attempts(job_id, attempt_no);
"""


class CampaignInitializationError(RuntimeError):
    """Raised when source data or an existing campaign is inconsistent."""


@dataclass(frozen=True)
class SourceCompany:
    cik: str
    ticker: str
    company_name: str
    sec_company_name: str
    sector: str
    sector_group: str
    cohort: str
    tier: int
    source_company_id: int
    source_db: str
    incorporation_code: str
    incorporation_normalized: str
    company_priority: int = 0


@dataclass(frozen=True)
class PilotSelection:
    rank: int
    stratum: str
    score: str


def now() -> str:
    return datetime.now(UTC).isoformat()


def readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def output_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    connection.executescript(SCHEMA)
    return connection


def normalize_cik(value: object) -> str:
    if isinstance(value, bool):
        raise CampaignInitializationError(f"invalid SEC CIK: {value!r}")
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip()
    else:
        raise CampaignInitializationError(f"invalid SEC CIK: {value!r}")
    if not re.fullmatch(r"[0-9]{1,10}", raw) or int(raw) == 0:
        raise CampaignInitializationError(f"invalid SEC CIK: {value!r}")
    return raw.zfill(10)


def normalize_incorporation(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = WHITESPACE.sub(" ", html.unescape(value)).strip()
    lowered = cleaned.casefold()
    if lowered in UNITED_STATES_LABELS:
        return "US"
    if lowered in US_STATE_NAME_TO_CODE:
        return US_STATE_NAME_TO_CODE[lowered]
    upper = cleaned.upper()
    if upper in US_STATE_CODES:
        return upper
    return None


def extract_incorporation_code(path: Path) -> str | None:
    """Read a consistent inline-XBRL DEI incorporation fact.

    Filings sometimes repeat the fact in hidden and visible contexts.  The
    streaming scan keeps memory bounded and de-duplicates chunk overlap.  A
    combined filing may legitimately contain several US registrants with
    different state codes; it remains in scope only when every repeated fact
    resolves to a US state/country code.
    """

    tail = b""
    bytes_read = 0
    seen_offsets: set[int] = set()
    values: list[str] = []
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            window = tail + chunk
            window_offset = bytes_read - len(tail)
            bytes_read += len(chunk)
            for match in INCORPORATION_FACT.finditer(window):
                absolute_offset = window_offset + match.start()
                if absolute_offset in seen_offsets:
                    continue
                seen_offsets.add(absolute_offset)
                plain = HTML_TAG.sub(b" ", match.group("body"))
                decoded = plain.decode("utf-8", errors="replace")
                cleaned = WHITESPACE.sub(" ", html.unescape(decoded)).strip()
                if cleaned:
                    values.append(cleaned)
            tail = window[-65536:]
    if not values:
        return None
    normalized = {normalize_incorporation(value) for value in values}
    if None in normalized:
        return None
    return values[0]


def resolve_filing_path(raw_path: str, annual_db: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute() and candidate.is_file():
        return candidate.resolve()
    bases = [Path.cwd(), annual_db.resolve().parent]
    bases.extend(list(annual_db.resolve().parents)[:3])
    for base in bases:
        resolved = (base / candidate).resolve()
        if resolved.is_file():
            return resolved
    raise CampaignInitializationError(
        f"10-K local_path does not resolve: {raw_path!r} from {annual_db}"
    )


def sector_group(sector: str | None) -> str:
    cleaned = WHITESPACE.sub(" ", (sector or "").strip())
    if not cleaned:
        return "Unknown"
    return cleaned.split("—", 1)[0].strip() or "Unknown"


def _source_cohort(path: Path, cohort: str, tier: int) -> dict[str, sqlite3.Row]:
    connection = readonly_connection(path)
    try:
        rows = connection.execute(
            "SELECT id,ticker,company_name,sector FROM companies ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise CampaignInitializationError(
            f"invalid {cohort} earnings source database {path}: {exc}"
        ) from exc
    finally:
        connection.close()
    result: dict[str, sqlite3.Row] = {}
    for row in rows:
        ticker = str(row["ticker"]).strip()
        if not ticker or ticker in result:
            raise CampaignInitializationError(
                f"empty or duplicate ticker {ticker!r} in {path}"
            )
        result[ticker] = row
    if not result:
        raise CampaignInitializationError(f"no companies found in {path}")
    return result


def load_strict_us_companies(
    annual_db: Path, top_db: Path, next_db: Path
) -> list[SourceCompany]:
    """Join local sources and return the strict, audited US-incorporated cohort."""

    cohort_rows: dict[str, tuple[str, int, Path, sqlite3.Row]] = {}
    for path, cohort, tier in ((top_db, "top", 1), (next_db, "next", 2)):
        for ticker, row in _source_cohort(path, cohort, tier).items():
            if ticker in cohort_rows:
                raise CampaignInitializationError(
                    f"ticker {ticker!r} appears in both cohort databases"
                )
            cohort_rows[ticker] = (cohort, tier, path.resolve(), row)

    annual = readonly_connection(annual_db)
    try:
        rows = annual.execute(
            """
            SELECT c.ticker,c.company_name AS sec_company_name,c.cik,f.local_path
            FROM companies c
            JOIN filings f ON f.company_id=c.id
            WHERE c.status='downloaded' AND f.form='10-K'
            ORDER BY c.id,f.id
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise CampaignInitializationError(
            f"invalid annual 10-K database {annual_db}: {exc}"
        ) from exc
    finally:
        annual.close()

    companies: list[SourceCompany] = []
    seen_ciks: set[str] = set()
    seen_tickers: set[str] = set()
    for row in rows:
        ticker = str(row["ticker"]).strip()
        source = cohort_rows.get(ticker)
        if source is None:
            raise CampaignInitializationError(
                f"annual 10-K ticker {ticker!r} is absent from top/next sources"
            )
        raw_code = extract_incorporation_code(
            resolve_filing_path(str(row["local_path"]), annual_db)
        )
        normalized = normalize_incorporation(raw_code)
        if normalized is None:
            continue
        cik = normalize_cik(row["cik"])
        if cik in seen_ciks or ticker in seen_tickers:
            raise CampaignInitializationError(
                f"duplicate strict-US identity cik={cik!r} ticker={ticker!r}"
            )
        seen_ciks.add(cik)
        seen_tickers.add(ticker)
        cohort, tier, source_path, source_row = source
        sector = str(source_row["sector"] or "").strip() or "Unknown"
        companies.append(
            SourceCompany(
                cik=cik,
                ticker=ticker,
                company_name=str(source_row["company_name"]).strip(),
                sec_company_name=str(row["sec_company_name"] or "").strip(),
                sector=sector,
                sector_group=sector_group(sector),
                cohort=cohort,
                tier=tier,
                source_company_id=int(source_row["id"]),
                source_db=str(source_path),
                incorporation_code=raw_code or "",
                incorporation_normalized=normalized,
            )
        )

    ordered = sorted(
        companies,
        key=lambda item: (item.tier, item.source_company_id, item.cik),
    )
    return [
        SourceCompany(**{**item.__dict__, "company_priority": rank})
        for rank, item in enumerate(ordered, 1)
    ]


def source_digest(companies: Sequence[SourceCompany]) -> str:
    digest = hashlib.sha256()
    for company in sorted(companies, key=lambda item: item.cik):
        fields = (
            company.cik,
            company.ticker,
            company.company_name,
            company.sec_company_name,
            company.sector,
            company.cohort,
            str(company.tier),
            str(company.source_company_id),
            company.incorporation_code,
            company.incorporation_normalized,
        )
        digest.update("\t".join(fields).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _apportion_strata(
    groups: dict[str, list[SourceCompany]], sample_size: int
) -> dict[str, int]:
    if sample_size > sum(len(members) for members in groups.values()):
        raise CampaignInitializationError(
            f"pilot size {sample_size} exceeds strict-US population"
        )
    if sample_size < len(groups):
        raise CampaignInitializationError(
            f"pilot size {sample_size} cannot represent all {len(groups)} strata"
        )
    allocation = {stratum: 1 for stratum in groups}
    remaining = sample_size - len(groups)
    capacity = {stratum: len(members) - 1 for stratum, members in groups.items()}
    total_capacity = sum(capacity.values())
    if remaining == 0:
        return allocation
    if total_capacity <= 0:
        raise CampaignInitializationError("pilot strata have no remaining capacity")

    fractions: list[tuple[float, str]] = []
    assigned = 0
    for stratum in sorted(groups):
        exact = remaining * capacity[stratum] / total_capacity
        extra = min(capacity[stratum], int(exact))
        allocation[stratum] += extra
        assigned += extra
        fractions.append((exact - int(exact), stratum))

    leftovers = remaining - assigned
    for _, stratum in sorted(fractions, key=lambda item: (-item[0], item[1])):
        if leftovers == 0:
            break
        if allocation[stratum] < len(groups[stratum]):
            allocation[stratum] += 1
            leftovers -= 1
    if leftovers:
        for stratum in sorted(groups):
            while leftovers and allocation[stratum] < len(groups[stratum]):
                allocation[stratum] += 1
                leftovers -= 1
    if leftovers or sum(allocation.values()) != sample_size:
        raise CampaignInitializationError("failed to apportion deterministic pilot")
    return allocation


def deterministic_pilot_sample(
    companies: Sequence[SourceCompany], sample_size: int, seed: str
) -> dict[str, PilotSelection]:
    groups: dict[str, list[SourceCompany]] = defaultdict(list)
    for company in companies:
        groups[f"{company.cohort}|{company.sector_group}"].append(company)
    if not groups:
        raise CampaignInitializationError("strict-US company universe is empty")
    allocation = _apportion_strata(groups, sample_size)

    selected: list[tuple[str, str, SourceCompany]] = []
    for stratum in sorted(groups):
        scored = []
        for company in groups[stratum]:
            score = hashlib.sha256(f"{seed}\0{company.cik}".encode()).hexdigest()
            scored.append((score, company.cik, company))
        for score, _, company in sorted(scored)[: allocation[stratum]]:
            selected.append((stratum, score, company))

    ordered = sorted(
        selected,
        key=lambda item: (
            item[2].tier,
            item[2].sector_group,
            item[1],
            item[2].cik,
        ),
    )
    return {
        company.cik: PilotSelection(rank, stratum, score)
        for rank, (stratum, score, company) in enumerate(ordered, 1)
    }


def campaign_periods(mode: str) -> list[tuple[int, str]]:
    if mode == "pilot":
        periods = [(year, "Q2") for year in range(2020, 2027)]
    elif mode == "full":
        periods = [
            (year, quarter)
            for year in range(2020, 2026)
            for quarter in VALID_QUARTERS
        ]
        periods.extend(((2026, "Q1"), (2026, "Q2")))
    else:
        raise CampaignInitializationError(f"unsupported campaign mode: {mode!r}")
    return sorted(
        periods,
        key=lambda item: (item[0], VALID_QUARTERS.index(item[1])),
        reverse=True,
    )


def _campaign_paths(annual_db: Path, top_db: Path, next_db: Path) -> tuple[str, ...]:
    return tuple(str(path.expanduser().resolve(strict=True)) for path in (annual_db, top_db, next_db))


def _validate_existing_campaign(
    row: sqlite3.Row,
    *,
    seed: str,
    pilot_size: int,
    paths: tuple[str, ...],
    digest: str,
) -> None:
    expected = {
        "schema_version": SCHEMA_VERSION,
        "seed": seed,
        "pilot_size": pilot_size,
        "annual_db": paths[0],
        "top_db": paths[1],
        "next_db": paths[2],
        "source_digest": digest,
    }
    mismatches = {
        key: (row[key], value)
        for key, value in expected.items()
        if row[key] != value
    }
    if mismatches:
        raise CampaignInitializationError(
            "existing campaign does not match requested immutable inputs: "
            + json.dumps(mismatches, sort_keys=True)
        )


def _insert_companies(
    connection: sqlite3.Connection,
    companies: Sequence[SourceCompany],
    sample: dict[str, PilotSelection],
    timestamp: str,
) -> int:
    inserted = 0
    for company in companies:
        selection = sample.get(company.cik)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO companies (
              cik,ticker,company_name,sec_company_name,sector,sector_group,
              cohort,tier,source_company_id,source_db,incorporation_code,
              incorporation_normalized,company_priority,pilot_selected,
              pilot_stratum,pilot_rank,sample_score,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                company.cik,
                company.ticker,
                company.company_name,
                company.sec_company_name,
                company.sector,
                company.sector_group,
                company.cohort,
                company.tier,
                company.source_company_id,
                company.source_db,
                company.incorporation_code,
                company.incorporation_normalized,
                company.company_priority,
                int(selection is not None),
                selection.stratum if selection else None,
                selection.rank if selection else None,
                selection.score if selection else None,
                timestamp,
                timestamp,
            ),
        )
        inserted += cursor.rowcount
    return inserted


def _insert_jobs(
    connection: sqlite3.Connection,
    companies: Iterable[SourceCompany],
    periods: Sequence[tuple[int, str]],
    timestamp: str,
) -> int:
    inserted = 0
    absolute_period_ranks = {
        period: rank for rank, period in enumerate(campaign_periods("full"))
    }
    for year, quarter in periods:
        period_rank = absolute_period_ranks[(year, quarter)]
        for company in companies:
            priority = period_rank * 100_000 + company.company_priority
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO jobs (
                  cik,calendar_year,quarter,calendar_target,
                  fiscal_year,fiscal_quarter,period_end,call_date,
                  reported_period_label,period_rank,priority,created_at,updated_at
                ) VALUES (?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?,?,?)
                """,
                (
                    company.cik,
                    year,
                    quarter,
                    f"{year}-{quarter}",
                    period_rank,
                    priority,
                    timestamp,
                    timestamp,
                ),
            )
            inserted += cursor.rowcount
    return inserted


def _statistics(
    connection: sqlite3.Connection,
    *,
    output_db: Path,
    requested_mode: str,
    effective_mode: str,
    digest: str,
    seed: str,
    jobs_inserted: int,
) -> dict[str, object]:
    cohorts = {
        row["cohort"]: row["count"]
        for row in connection.execute(
            "SELECT cohort,COUNT(*) AS count FROM companies GROUP BY cohort"
        )
    }
    pilot_strata = {
        row["pilot_stratum"]: row["count"]
        for row in connection.execute(
            """
            SELECT pilot_stratum,COUNT(*) AS count
            FROM companies WHERE pilot_selected=1
            GROUP BY pilot_stratum ORDER BY pilot_stratum
            """
        )
    }
    jobs_by_period = {
        row["calendar_target"]: row["count"]
        for row in connection.execute(
            """
            SELECT calendar_target,COUNT(*) AS count
            FROM jobs GROUP BY calendar_year,quarter,calendar_target
            ORDER BY calendar_year DESC,
              CASE quarter WHEN 'Q4' THEN 4 WHEN 'Q3' THEN 3
                WHEN 'Q2' THEN 2 ELSE 1 END DESC
            """
        )
    }
    job_status = {
        row["status"]: row["count"]
        for row in connection.execute(
            "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status"
        )
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "output_db": str(output_db.expanduser().resolve()),
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
        "seed": seed,
        "source_digest": digest,
        "strict_us_companies": connection.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0],
        "cohorts": cohorts,
        "pilot_selected": connection.execute(
            "SELECT COUNT(*) FROM companies WHERE pilot_selected=1"
        ).fetchone()[0],
        "pilot_strata": pilot_strata,
        "periods": len(jobs_by_period),
        "jobs_inserted": jobs_inserted,
        "jobs_total": connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "jobs_by_period": jobs_by_period,
        "job_status": job_status,
        "search_attempts": connection.execute(
            "SELECT COUNT(*) FROM search_attempts"
        ).fetchone()[0],
    }


def initialize_campaign(
    *,
    annual_db: Path,
    top_db: Path,
    next_db: Path,
    output_db: Path,
    mode: str = "pilot",
    seed: str = DEFAULT_SEED,
    pilot_size: int = DEFAULT_PILOT_SIZE,
) -> dict[str, object]:
    """Initialize or idempotently resume one campaign metadata database."""

    if mode not in {"pilot", "full"}:
        raise CampaignInitializationError(f"unsupported campaign mode: {mode!r}")
    if pilot_size < 1:
        raise CampaignInitializationError("pilot_size must be positive")
    started_at = now()
    paths = _campaign_paths(annual_db, top_db, next_db)
    companies = load_strict_us_companies(annual_db, top_db, next_db)
    if not companies:
        raise CampaignInitializationError("no strict-US companies found")
    digest = source_digest(companies)
    sample = deterministic_pilot_sample(companies, pilot_size, seed)

    connection = output_connection(output_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM campaign WHERE id=1").fetchone()
        if existing is not None:
            _validate_existing_campaign(
                existing,
                seed=seed,
                pilot_size=pilot_size,
                paths=paths,
                digest=digest,
            )
            effective_mode = (
                "full" if existing["mode"] == "full" or mode == "full" else "pilot"
            )
        else:
            effective_mode = mode

        timestamp = now()
        if existing is None:
            connection.execute(
                """
                INSERT INTO campaign (
                  id,schema_version,mode,seed,pilot_size,annual_db,top_db,next_db,
                  source_digest,created_at,updated_at
                ) VALUES (1,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    SCHEMA_VERSION,
                    effective_mode,
                    seed,
                    pilot_size,
                    *paths,
                    digest,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            connection.execute(
                "UPDATE campaign SET mode=?,updated_at=? WHERE id=1",
                (effective_mode, timestamp),
            )

        _insert_companies(connection, companies, sample, timestamp)
        expected_ciks = {company.cik for company in companies}
        stored_ciks = {
            row["cik"] for row in connection.execute("SELECT cik FROM companies")
        }
        if stored_ciks != expected_ciks:
            raise CampaignInitializationError(
                "campaign companies differ from the immutable source universe"
            )
        stored_sample = {
            row["cik"]
            for row in connection.execute(
                "SELECT cik FROM companies WHERE pilot_selected=1"
            )
        }
        if stored_sample != set(sample):
            raise CampaignInitializationError(
                "campaign pilot sample differs from the deterministic selection"
            )
        job_companies = (
            companies
            if effective_mode == "full"
            else [company for company in companies if company.cik in sample]
        )
        periods = campaign_periods(effective_mode)
        jobs_inserted = _insert_jobs(connection, job_companies, periods, timestamp)
        expected_jobs = len(job_companies) * len(periods)
        actual_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if actual_jobs != expected_jobs:
            raise CampaignInitializationError(
                f"campaign has {actual_jobs} jobs; expected exactly {expected_jobs}"
            )
        stats = _statistics(
            connection,
            output_db=output_db,
            requested_mode=mode,
            effective_mode=effective_mode,
            digest=digest,
            seed=seed,
            jobs_inserted=jobs_inserted,
        )
        finished_at = now()
        connection.execute(
            """
            INSERT INTO initialization_runs (
              requested_mode,effective_mode,seed,source_digest,
              strict_us_companies,pilot_selected,periods,jobs_inserted,jobs_total,
              statistics_json,started_at,finished_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                mode,
                effective_mode,
                seed,
                digest,
                stats["strict_us_companies"],
                stats["pilot_selected"],
                stats["periods"],
                stats["jobs_inserted"],
                stats["jobs_total"],
                json.dumps(stats, sort_keys=True),
                started_at,
                finished_at,
            ),
        )
        connection.execute("COMMIT")
        return stats
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize a US earnings-call history campaign; never search."
    )
    parser.add_argument("--annual-db", type=Path, required=True)
    parser.add_argument("--top-db", type=Path, required=True)
    parser.add_argument("--next-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--mode", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--pilot-size", type=int, default=DEFAULT_PILOT_SIZE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.pilot_size < 1:
        raise SystemExit("--pilot-size must be positive")
    statistics = initialize_campaign(
        annual_db=args.annual_db,
        top_db=args.top_db,
        next_db=args.next_db,
        output_db=args.output_db,
        mode=args.mode,
        seed=args.seed,
        pilot_size=args.pilot_size,
    )
    print(json.dumps(statistics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
