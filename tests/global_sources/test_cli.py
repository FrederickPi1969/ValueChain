from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from gcu_priority_markets.cli import app


runner = CliRunner()


def test_cli_markets() -> None:
    result = runner.invoke(app, ["markets"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["markets"]) == 15


def test_cli_offline_smoke(tmp_path: Path) -> None:
    output = tmp_path / "smoke.json"
    result = runner.invoke(app, ["smoke", "--offline", "--output", str(output)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["checked"] == 21
    assert payload["failed"] == 0


def test_cli_official_export_universe(fixture_dir: Path, tmp_path: Path) -> None:
    output = tmp_path / "six.csv"
    result = runner.invoke(
        app,
        [
            "universe",
            "--source",
            "six_exchange",
            "--input-path",
            str(fixture_dir / "six_issuers.csv"),
            "--output-csv",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert output.exists()
    assert len(output.read_text(encoding="utf-8").splitlines()) == 3


def test_cli_sedar_alerts(fixture_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    result = runner.invoke(
        app,
        [
            "sedar-alerts",
            "--mail-path",
            str(fixture_dir / "sedar_alert.eml"),
            "--state-file",
            str(state),
            "--events-file",
            str(events),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert len(events.read_text(encoding="utf-8").splitlines()) == 1
