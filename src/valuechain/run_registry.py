from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from valuechain.config import Settings
from valuechain.io_utils import write_json


REGISTRY_FILENAME = "runs.json"


def make_run_id(prefix: str = "run") -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return normalize_run_id(f"{timestamp}_{prefix}")


def normalize_run_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or make_run_id("run")


def update_run_registry(
    settings: Settings,
    run_id: str,
    run_label: str,
    summary: dict[str, Any],
    dashboard_path: Path,
    processed_dir: Path,
) -> list[dict[str, Any]]:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    registry_path = settings.reports_dir / REGISTRY_FILENAME
    runs = read_run_registry(registry_path)
    rel_dashboard = dashboard_path.relative_to(settings.reports_dir)
    rel_processed = processed_dir.relative_to(settings.processed_dir)
    entry = {
        "run_id": run_id,
        "run_label": run_label or run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dashboard": str(rel_dashboard),
        "data_path": f"/data/runs/{run_id}/dashboard-data.json",
        "processed_dir": str(rel_processed),
        "counts": summary.get("counts", {}),
        "options": summary.get("options", {}),
    }
    runs = [run for run in runs if run.get("run_id") != run_id]
    runs.insert(0, entry)
    runs.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    registry_path.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=2), encoding="utf-8")
    render_run_index(settings, runs)
    sync_frontend_public_data(settings, runs)
    return runs


def read_run_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    runs = payload.get("runs", [])
    return runs if isinstance(runs, list) else []


def render_run_index(settings: Settings, runs: list[dict[str, Any]] | None = None) -> Path:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    if runs is None:
        runs = read_run_registry(settings.reports_dir / REGISTRY_FILENAME)
    template_dir = Path(__file__).resolve().parents[2] / "templates"
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("index.html.j2")
    index_path = settings.reports_dir / "index.html"
    index_path.write_text(template.render(runs=runs), encoding="utf-8")
    return index_path


def copy_latest_dashboard(settings: Settings, dashboard_path: Path) -> Path:
    latest_path = settings.reports_dir / "dashboard.html"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dashboard_path, latest_path)
    return latest_path


def copy_latest_processed_outputs(processed_dir: Path, latest_dir: Path) -> None:
    latest_dir.mkdir(parents=True, exist_ok=True)
    for path in processed_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, latest_dir / path.name)


def sync_frontend_public_data(settings: Settings, runs: list[dict[str, Any]]) -> None:
    public_data_dir = settings.root_dir / "frontend" / "public" / "data"
    if not (settings.root_dir / "frontend").exists():
        return
    public_data_dir.mkdir(parents=True, exist_ok=True)
    write_json(public_data_dir / REGISTRY_FILENAME, {"runs": runs})
    for run in runs:
        run_id = str(run.get("run_id", ""))
        dashboard_rel = str(run.get("dashboard", ""))
        if not run_id or not dashboard_rel:
            continue
        source = (settings.reports_dir / dashboard_rel).parent / "dashboard-data.json"
        if not source.exists():
            continue
        target_dir = public_data_dir / "runs" / run_id
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_dir / "dashboard-data.json")
        sync_frontend_public_briefs(settings, run_id)


def sync_frontend_public_briefs(settings: Settings, run_id: str) -> list[dict[str, Any]]:
    """Copy generated company briefs into Vite static data and write a small index."""

    frontend_dir = settings.root_dir / "frontend"
    if not frontend_dir.exists():
        return []
    source_dir = settings.reports_dir / "runs" / run_id / "briefs"
    if not source_dir.exists():
        return []

    target_dir = frontend_dir / "public" / "data" / "runs" / run_id / "briefs"
    target_dir.mkdir(parents=True, exist_ok=True)
    for stale in target_dir.glob("*_dependency_brief.json"):
        stale.unlink()

    rows: list[dict[str, Any]] = []
    for source in sorted(source_dir.rglob("*_dependency_brief.json")):
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        company = payload.get("company") if isinstance(payload.get("company"), dict) else {}
        interpretation = payload.get("analyst_interpretation")
        if not isinstance(interpretation, dict):
            interpretation = {}
        diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
        ticker = str(company.get("ticker") or source.name.split("_", 1)[0]).upper()
        target_name = f"{ticker}_dependency_brief.json"
        shutil.copy2(source, target_dir / target_name)
        rows.append(
            {
                "ticker": ticker,
                "company_name": company.get("company_name") or ticker,
                "role": company.get("role") or "",
                "priority": company.get("priority") or "",
                "path": f"/data/runs/{run_id}/briefs/{target_name}",
                "model_version": payload.get("model_version") or interpretation.get("model_version") or "",
                "claim_count": diagnostics.get("claim_count", 0),
                "evidence_count": len(payload.get("evidence_table") or []),
                "summary": interpretation.get("one_paragraph_summary") or "",
            }
        )

    rows.sort(key=lambda row: (int(row["priority"]) if str(row["priority"]).isdigit() else 999, row["ticker"]))
    write_json(target_dir / "index.json", {"run_id": run_id, "briefs": rows})
    return rows
