from valuechain.config import Settings
from valuechain.run_registry import normalize_run_id, sync_frontend_public_data, update_run_registry


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
