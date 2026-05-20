from valuechain.config import Settings
import json

from valuechain.run_registry import (
    normalize_run_id,
    sync_frontend_public_briefs,
    sync_frontend_public_data,
    update_run_registry,
)


def test_normalize_run_id_removes_unsafe_chars() -> None:
    assert normalize_run_id("AI Run: priority 1 / 10-K") == "AI-Run-priority-1-10-K"


def test_update_run_registry_writes_index_and_frontend_data(tmp_path) -> None:
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        raw_dir=tmp_path / "data" / "raw",
        processed_dir=tmp_path / "data" / "processed",
        reports_dir=tmp_path / "reports",
    )
    (tmp_path / "frontend" / "public" / "data").mkdir(parents=True)
    dashboard_dir = settings.reports_dir / "runs" / "r1"
    dashboard_dir.mkdir(parents=True)
    dashboard_path = dashboard_dir / "dashboard.html"
    dashboard_path.write_text("<html></html>", encoding="utf-8")
    (dashboard_dir / "dashboard-data.json").write_text('{"summary":{}}', encoding="utf-8")
    processed_dir = settings.processed_dir / "runs" / "r1"
    processed_dir.mkdir(parents=True)

    runs = update_run_registry(
        settings,
        run_id="r1",
        run_label="Priority 1",
        summary={"counts": {"companies": 2}, "options": {"extractor": "rules"}},
        dashboard_path=dashboard_path,
        processed_dir=processed_dir,
    )
    sync_frontend_public_data(settings, runs)

    assert (settings.reports_dir / "index.html").exists()
    assert (tmp_path / "frontend" / "public" / "data" / "runs.json").exists()
    assert (tmp_path / "frontend" / "public" / "data" / "runs" / "r1" / "dashboard-data.json").exists()


def test_sync_frontend_public_briefs_writes_index(tmp_path) -> None:
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        raw_dir=tmp_path / "data" / "raw",
        processed_dir=tmp_path / "data" / "processed",
        reports_dir=tmp_path / "reports",
    )
    (tmp_path / "frontend" / "public" / "data").mkdir(parents=True)
    briefs_dir = settings.reports_dir / "runs" / "r1" / "briefs"
    briefs_dir.mkdir(parents=True)
    (briefs_dir / "NVDA_dependency_brief.json").write_text(
        json.dumps(
            {
                "company": {
                    "ticker": "NVDA",
                    "company_name": "NVIDIA Corporation",
                    "role": "accelerator_compute",
                    "priority": "1",
                },
                "analyst_interpretation": {
                    "model_version": "Qwen/Qwen3.6-35B-A3B",
                    "one_paragraph_summary": "NVIDIA depends on foundry and packaging partners.",
                },
                "diagnostics": {"claim_count": 4},
                "evidence_table": [{"evidence_id": "E1"}, {"evidence_id": "E2"}],
            }
        ),
        encoding="utf-8",
    )

    rows = sync_frontend_public_briefs(settings, "r1")
    target_dir = tmp_path / "frontend" / "public" / "data" / "runs" / "r1" / "briefs"
    index = json.loads((target_dir / "index.json").read_text(encoding="utf-8"))

    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["evidence_count"] == 2
    assert index["briefs"][0]["path"] == "/data/runs/r1/briefs/NVDA_dependency_brief.json"
    assert (target_dir / "NVDA_dependency_brief.json").exists()
