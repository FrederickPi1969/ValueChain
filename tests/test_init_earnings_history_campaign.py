from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from init_earnings_history_campaign import (
    CampaignInitializationError,
    deterministic_pilot_sample,
    extract_incorporation_code,
    initialize_campaign,
    load_strict_us_companies,
    normalize_cik,
    normalize_incorporation,
)


def build_real_scale_sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    annual_db = tmp_path / "annual.sqlite3"
    top_db = tmp_path / "top.sqlite3"
    next_db = tmp_path / "next.sqlite3"
    filing = tmp_path / "shared-10k.html"
    filing.write_text(
        """
        <html><body>
          <ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">
            <span>Delaware</span>
          </ix:nonNumeric>
        </body></html>
        """,
        encoding="utf-8",
    )

    cohorts = (
        (top_db, "TOP", 882, ("Technology — Software", "Industrials — Equipment")),
        (next_db, "NEXT", 711, ("Financials — Banking", "Energy — Production")),
    )
    annual_rows: list[tuple[int, str, str, str, str]] = []
    filing_rows: list[tuple[int, int, str, str]] = []
    global_id = 0
    for database, prefix, count, sectors in cohorts:
        connection = sqlite3.connect(database)
        connection.execute(
            """
            CREATE TABLE companies (
              id INTEGER PRIMARY KEY,
              ticker TEXT NOT NULL,
              company_name TEXT NOT NULL,
              sector TEXT
            )
            """
        )
        for source_id in range(1, count + 1):
            global_id += 1
            ticker = f"{prefix}{source_id:04d}"
            name = f"{prefix} Company {source_id}"
            sector = sectors[(source_id - 1) % len(sectors)]
            connection.execute(
                "INSERT INTO companies(id,ticker,company_name,sector) VALUES (?,?,?,?)",
                (source_id, ticker, name, sector),
            )
            cik = f"{global_id:010d}"
            annual_rows.append((global_id, ticker, f"SEC {name}", cik, "downloaded"))
            filing_rows.append((global_id, global_id, "10-K", str(filing)))
        connection.commit()
        connection.close()

    annual = sqlite3.connect(annual_db)
    annual.executescript(
        """
        CREATE TABLE companies (
          id INTEGER PRIMARY KEY,
          ticker TEXT NOT NULL,
          company_name TEXT,
          cik TEXT,
          status TEXT NOT NULL
        );
        CREATE TABLE filings (
          id INTEGER PRIMARY KEY,
          company_id INTEGER NOT NULL,
          form TEXT NOT NULL,
          local_path TEXT
        );
        """
    )
    annual.executemany(
        "INSERT INTO companies(id,ticker,company_name,cik,status) VALUES (?,?,?,?,?)",
        annual_rows,
    )
    annual.executemany(
        "INSERT INTO filings(id,company_id,form,local_path) VALUES (?,?,?,?)",
        filing_rows,
    )
    annual.commit()
    annual.close()
    return annual_db, top_db, next_db


def test_pilot_is_deterministic_idempotent_and_promotes_to_full(tmp_path: Path) -> None:
    annual_db, top_db, next_db = build_real_scale_sources(tmp_path)
    campaign_db = tmp_path / "campaign.sqlite3"
    inputs = {
        "annual_db": annual_db,
        "top_db": top_db,
        "next_db": next_db,
        "output_db": campaign_db,
        "seed": "fixed-test-seed",
    }

    first = initialize_campaign(**inputs)
    assert first["requested_mode"] == "pilot"
    assert first["effective_mode"] == "pilot"
    assert first["strict_us_companies"] == 1593
    assert first["cohorts"] == {"top": 882, "next": 711}
    assert first["pilot_selected"] == 100
    assert len(first["pilot_strata"]) == 4
    assert all(count > 0 for count in first["pilot_strata"].values())
    assert first["periods"] == 7
    assert first["jobs_inserted"] == 700
    assert first["jobs_total"] == 700
    assert set(first["jobs_by_period"]) == {
        f"{year}-Q2" for year in range(2020, 2027)
    }
    assert set(first["jobs_by_period"].values()) == {100}
    assert first["search_attempts"] == 0

    db = sqlite3.connect(campaign_db)
    db.row_factory = sqlite3.Row
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute(
        """
        SELECT COUNT(*) FROM jobs
        WHERE fiscal_year IS NULL AND fiscal_quarter IS NULL
          AND period_end IS NULL AND call_date IS NULL
          AND reported_period_label IS NULL
        """
    ).fetchone()[0] == 700
    assert db.execute(
        "SELECT calendar_target FROM jobs ORDER BY priority LIMIT 1"
    ).fetchone()[0] == "2026-Q2"
    assert db.execute(
        "SELECT calendar_target FROM jobs ORDER BY priority DESC LIMIT 1"
    ).fetchone()[0] == "2020-Q2"

    lease_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(jobs)")
    }
    assert {
        "status",
        "stage",
        "attempt_count",
        "retry_count",
        "max_attempts",
        "next_attempt_at",
        "lease_owner",
        "lease_token",
        "lease_acquired_at",
        "lease_expires_at",
        "heartbeat_at",
        "last_error",
    } <= lease_columns

    preserved_job = db.execute("SELECT id FROM jobs ORDER BY priority LIMIT 1").fetchone()[0]
    db.execute(
        """
        UPDATE jobs SET status='running',stage='download',query_count=1,
          attempt_count=2,retry_count=1,lease_owner='worker-a',
          lease_token='lease-a',lease_expires_at='2099-01-01T00:00:00+00:00'
        WHERE id=?
        """,
        (preserved_job,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE jobs SET query_count=5 WHERE id=?", (preserved_job,))
    db.commit()
    selected_once = {
        row[0]
        for row in db.execute("SELECT cik FROM companies WHERE pilot_selected=1")
    }
    db.close()

    second = initialize_campaign(**inputs)
    assert second["jobs_inserted"] == 0
    assert second["jobs_total"] == 700
    db = sqlite3.connect(campaign_db)
    db.row_factory = sqlite3.Row
    preserved = db.execute("SELECT * FROM jobs WHERE id=?", (preserved_job,)).fetchone()
    assert preserved["status"] == "running"
    assert preserved["stage"] == "download"
    assert preserved["query_count"] == 1
    assert preserved["attempt_count"] == 2
    assert preserved["lease_token"] == "lease-a"
    assert db.execute("SELECT COUNT(*) FROM initialization_runs").fetchone()[0] == 2
    db.close()

    other_db = tmp_path / "same-seed.sqlite3"
    initialize_campaign(**{**inputs, "output_db": other_db})
    other = sqlite3.connect(other_db)
    selected_twice = {
        row[0]
        for row in other.execute("SELECT cik FROM companies WHERE pilot_selected=1")
    }
    other.close()
    assert selected_once == selected_twice

    companies = load_strict_us_companies(annual_db, top_db, next_db)
    assert deterministic_pilot_sample(companies, 100, "fixed-test-seed") == (
        deterministic_pilot_sample(list(reversed(companies)), 100, "fixed-test-seed")
    )
    assert set(deterministic_pilot_sample(companies, 100, "different-seed")) != (
        selected_once
    )

    promoted = initialize_campaign(**inputs, mode="full")
    assert promoted["requested_mode"] == "full"
    assert promoted["effective_mode"] == "full"
    assert promoted["periods"] == 26
    assert promoted["jobs_inserted"] == 41_418 - 700
    assert promoted["jobs_total"] == 41_418
    assert set(promoted["jobs_by_period"].values()) == {1593}

    db = sqlite3.connect(campaign_db)
    db.row_factory = sqlite3.Row
    assert db.execute(
        "SELECT COUNT(*) FROM jobs GROUP BY cik HAVING COUNT(*)<>26"
    ).fetchone() is None
    assert db.execute(
        "SELECT calendar_target FROM jobs ORDER BY priority LIMIT 1"
    ).fetchone()[0] == "2026-Q2"
    assert db.execute(
        "SELECT calendar_target FROM jobs ORDER BY priority DESC LIMIT 1"
    ).fetchone()[0] == "2020-Q1"
    preserved = db.execute("SELECT * FROM jobs WHERE id=?", (preserved_job,)).fetchone()
    assert preserved["status"] == "running"
    assert preserved["attempt_count"] == 2
    assert db.execute("SELECT mode FROM campaign WHERE id=1").fetchone()[0] == "full"
    assert db.execute("SELECT COUNT(*) FROM search_attempts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM job_attempts").fetchone()[0] == 0
    db.close()

    full_rerun = initialize_campaign(**inputs, mode="full")
    assert full_rerun["jobs_inserted"] == 0
    assert full_rerun["jobs_total"] == 41_418
    with pytest.raises(CampaignInitializationError, match="immutable inputs"):
        initialize_campaign(**{**inputs, "seed": "changed-seed"}, mode="full")


@pytest.mark.parametrize(
    ("markup", "raw", "normalized"),
    [
        (
            (
                '<ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">'
                "<span>Washington</span></ix:nonNumeric>"
            ),
            "Washington",
            "WA",
        ),
        (
            (
                "<ix:nonNumeric name='dei:EntityIncorporationStateCountryCode'>"
                "New&#160;Jersey</ix:nonNumeric>"
            ),
            "New Jersey",
            "NJ",
        ),
        (
            (
                '<ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">'
                "District of Columbia</ix:nonNumeric>"
            ),
            "District of Columbia",
            "DC",
        ),
        (
            (
                '<ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">'
                "United States</ix:nonNumeric>"
            ),
            "United States",
            "US",
        ),
    ],
)
def test_nested_xbrl_incorporation_extraction(
    tmp_path: Path, markup: str, raw: str, normalized: str
) -> None:
    filing = tmp_path / "filing.html"
    filing.write_text(f"<html>{markup}</html>", encoding="utf-8")
    assert extract_incorporation_code(filing) == raw
    assert normalize_incorporation(raw) == normalized


def test_repeated_incorporation_facts_allow_us_registrants_and_reject_foreign(
    tmp_path: Path,
) -> None:
    filing = tmp_path / "repeated.html"
    filing.write_text(
        """
        <ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">Delaware</ix:nonNumeric>
        <ix:nonNumeric name="dei:EntityIncorporationStateCountryCode"><span>DE</span></ix:nonNumeric>
        """,
        encoding="utf-8",
    )
    assert normalize_incorporation(extract_incorporation_code(filing)) == "DE"

    filing.write_text(
        """
        <ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">Delaware</ix:nonNumeric>
        <ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">Maryland</ix:nonNumeric>
        """,
        encoding="utf-8",
    )
    assert normalize_incorporation(extract_incorporation_code(filing)) == "DE"

    filing.write_text(
        """
        <ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">Delaware</ix:nonNumeric>
        <ix:nonNumeric name="dei:EntityIncorporationStateCountryCode">Ireland</ix:nonNumeric>
        """,
        encoding="utf-8",
    )
    assert extract_incorporation_code(filing) is None


@pytest.mark.parametrize(
    "foreign_or_territory",
    ["Ireland", "Jersey", "Puerto Rico", "Virgin Islands", "Bermuda"],
)
def test_strict_us_filter_rejects_foreign_and_territories(
    foreign_or_territory: str,
) -> None:
    assert normalize_incorporation(foreign_or_territory) is None


def test_cik_normalization_is_strict() -> None:
    assert normalize_cik(789019) == "0000789019"
    assert normalize_cik(" 0000789019 ") == "0000789019"
    for invalid in (None, "", "0", 0, "CIK 789019", "123.0", -1, 1.5, True):
        with pytest.raises(CampaignInitializationError, match="invalid SEC CIK"):
            normalize_cik(invalid)
