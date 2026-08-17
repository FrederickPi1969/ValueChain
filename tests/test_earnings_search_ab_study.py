import asyncio
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_earnings_search_ab_study import (
    DownloaderTrialRunner,
    SearchResponse,
    StudyCompany,
    TrialOutcome,
    build_report,
    connect,
    exact_mcnemar_p,
    initialize_study,
    normalize_url,
    query_texts,
    run_workers,
)

from valuechain.earnings_calls import Candidate, Judgement


class FakeBackend:
    def __init__(self, name: str, results: dict[int, list[str]]) -> None:
        self.name = name
        self.results = results
        self.calls: list[str] = []

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        self.calls.append(query)
        ordinal = len(self.calls)
        return SearchResponse(
            [
                Candidate(url, f"result {url}", "earnings call transcript", self.name, query)
                for url in self.results.get(ordinal, [])[:limit]
            ],
            request_id=f"{self.name}-{ordinal}",
        )


async def fake_judge(
    company: str, year: int, quarter: str, candidates: list[Candidate]
) -> list[Judgement]:
    return [
        Judgement(
            index,
            True,
            0.99,
            "third_party_transcript",
            "mock high-confidence candidate",
        )
        for index, _ in enumerate(candidates)
    ]


async def fake_trial(company, candidate, judgement) -> TrialOutcome:
    if candidate["url"].endswith("/complete"):
        return TrialOutcome(
            "validated",
            fetch_method="mock",
            text_chars=50_000,
            artifact_path="mock.zst",
            validation={"full_call": True, "period_match": True},
        )
    return TrialOutcome(
        "rejected",
        fetch_method="mock",
        text_chars=20_000,
        validation={"full_call": False, "document_type": "partial_call"},
    )


def one_company() -> list[StudyCompany]:
    return [
        StudyCompany(
            tier="top",
            stratum="validated",
            source_db="mock.sqlite",
            source_company_id=1,
            ticker="MOCK",
            company_name="Mock Corporation",
            sector="Technology",
            population_n=52,
        )
    ]


def test_independent_arms_stop_only_after_strict_trial_success_and_resume(tmp_path: Path) -> None:
    database = tmp_path / "study.sqlite3"
    initialize_study(database, one_company(), year=2026, quarter="Q1", seed="test")
    google = FakeBackend(
        "google",
        {
            1: ["https://example.com/high-confidence-partial"],
            2: ["https://example.com/complete"],
            3: ["https://example.com/must-not-run"],
        },
    )
    ddg = FakeBackend(
        "duckduckgo",
        {
            1: ["https://ddg.example.com/complete"],
            2: ["https://ddg.example.com/must-not-run"],
        },
    )
    asyncio.run(
        run_workers(
            database,
            backends={"google": google, "duckduckgo": ddg},
            trial_fn=fake_trial,
            judge_fn=fake_judge,
            year=2026,
            quarter="Q1",
            workers=2,
        )
    )

    assert len(google.calls) == 2
    assert len(ddg.calls) == 1
    assert google.calls[0] == ddg.calls[0]
    db = connect(database)
    try:
        rows = {
            row["engine"]: row
            for row in db.execute("SELECT * FROM arms ORDER BY engine")
        }
        assert rows["google"]["success_query_ordinal"] == 2
        assert rows["duckduckgo"]["success_query_ordinal"] == 1
        assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM judgements").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 3
        assert db.execute(
            "SELECT COUNT(*) FROM trials WHERE status='rejected'"
        ).fetchone()[0] == 1
    finally:
        db.close()

    # A plain resume never consumes the same search request again.
    asyncio.run(
        run_workers(
            database,
            backends={"google": google, "duckduckgo": ddg},
            trial_fn=fake_trial,
            judge_fn=fake_judge,
            year=2026,
            quarter="Q1",
            workers=2,
        )
    )
    assert len(google.calls) == 2
    assert len(ddg.calls) == 1


def test_report_has_weighted_paired_metrics_and_quota_savings(tmp_path: Path) -> None:
    database = tmp_path / "report.sqlite3"
    patterns = {
        ("top", "validated"): (1, 1, 1, 1),
        ("top", "exhausted"): (1, 0, 2, 4),
        ("top", "no_accepted"): (0, 1, 4, 2),
        ("next", "validated"): (0, 0, 4, 4),
        ("next", "exhausted"): (1, 1, 3, 1),
        ("next", "no_accepted"): (0, 1, 4, 3),
    }
    population = {
        ("top", "validated"): 52,
        ("top", "exhausted"): 598,
        ("top", "no_accepted"): 350,
        ("next", "validated"): 372,
        ("next", "exhausted"): 438,
        ("next", "no_accepted"): 190,
    }
    companies = [
        StudyCompany(
            tier=tier,
            stratum=stratum,
            source_db="mock",
            source_company_id=index,
            ticker=f"T{index}",
            company_name=f"Test {index}",
            sector="Mock",
            population_n=population[(tier, stratum)],
        )
        for index, (tier, stratum) in enumerate(patterns, 1)
    ]
    initialize_study(database, companies, year=2026, quarter="Q1", seed="report")
    db = connect(database)
    try:
        for row in db.execute(
            "SELECT a.company_id,a.engine,c.tier,c.stratum FROM arms a "
            "JOIN sample_companies c ON c.id=a.company_id"
        ).fetchall():
            google, ddg, google_queries, ddg_queries = patterns[
                (row["tier"], row["stratum"])
            ]
            success = google if row["engine"] == "google" else ddg
            queries = google_queries if row["engine"] == "google" else ddg_queries
            db.execute(
                "UPDATE arms SET status='completed',success=?,success_query_ordinal=?,"
                "query_count=?,finished_at=?,updated_at=? WHERE company_id=? AND engine=?",
                (
                    success,
                    queries if success else None,
                    queries,
                    "done",
                    "done",
                    row["company_id"],
                    row["engine"],
                ),
            )
    finally:
        db.close()

    report = build_report(database, bootstrap_replicates=100, seed="report")
    assert report["completed_pairs"] == 6
    assert report["paired_overlap"] == {
        "both": 2,
        "google_only": 1,
        "duckduckgo_only": 2,
        "neither": 1,
        "union_success": 5,
        "normalized_url_overlap": 0,
    }
    assert report["weighted_success_rate"]["google"] == 1088 / 2000
    assert report["weighted_success_rate"]["duckduckgo"] == 1030 / 2000
    assert report["weighted_success_rate"]["union"] == 1628 / 2000
    assert report["ddg_first_google_quota"]["raw_fraction_saved"] == pytest.approx(12 / 18)
    assert report["mcnemar_exact_two_sided_p"] == exact_mcnemar_p(1, 2)
    low, high = report["bootstrap_95pct_ci_duckduckgo_minus_google"]
    assert low is not None and high is not None and low <= high


def test_query_and_url_contracts_are_engine_neutral() -> None:
    queries = query_texts("SMFG", "Sumitomo Mitsui Financial Group", 2026, "q1")
    assert queries == [
        "SMFG Sumitomo Mitsui Financial Group 2026 Q1 earnings conference call",
        "SMFG Sumitomo Mitsui Financial Group 2026 Q1 earnings conference call YouTube",
        "SMFG Sumitomo Mitsui Financial Group 2026 Q1 earnings call transcript",
        "SMFG Sumitomo Mitsui Financial Group 2026 Q1 quarterly results conference call",
    ]
    assert normalize_url("https://youtu.be/uBxX_QjbfGo?utm_source=x") == (
        "https://youtube.com/watch?v=uBxX_QjbfGo"
    )
    assert normalize_url(
        "https://Example.com/call/?utm_source=x&quarter=Q1#transcript"
    ) == "https://example.com/call?quarter=Q1"


def test_study_artifacts_are_independently_zstd_compressed(tmp_path: Path) -> None:
    runner = DownloaderTrialRunner(
        client=None,  # _persist_artifact is deliberately network-free.
        artifact_root=tmp_path,
        year=2026,
        quarter="Q1",
        extractor=None,
        fallback_lock=asyncio.Lock(),
    )
    staging = tmp_path / "staging"
    target = tmp_path / "target"
    staging.mkdir()
    (staging / "source.pdf").write_bytes(b"%PDF-mock" * 1_000)
    text = (
        "Operator: Welcome to the Q1 2026 earnings conference call. "
        "Management discussed revenue, customers, suppliers, and outlook. "
        "Question-and-answer session followed. This concludes today's conference call.\n"
        * 120
    )
    artifact = runner._persist_artifact(
        staging,
        target,
        {"ticker": "MOCK", "company_name": "Mock Corp"},
        {
            "id": 7,
            "engine": "google",
            "query_ordinal": 1,
            "url": "https://example.com/call.pdf",
        },
        Judgement(0, True, 0.99, "official_transcript", "mock"),
        text,
        "mock",
        {},
        None,
        {
            "full_call": True,
            "period_match": True,
            "document_type": "earnings_call",
            "confidence": 0.99,
        },
        True,
    )
    assert artifact.name == "transcript.txt.zst"
    assert {path.name for path in target.iterdir()} == {
        "source.pdf.zst",
        "transcript.txt.zst",
        "metadata.json.zst",
        "manifest.json.zst",
    }
    assert not any(path.suffix in {".txt", ".json", ".pdf"} for path in target.iterdir())
