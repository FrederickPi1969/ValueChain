from pathlib import Path

from valuechain.config import ROOT, Settings, ensure_dirs


def test_storage_paths_default_to_repository() -> None:
    settings = Settings()

    assert settings.data_dir == ROOT / "data"
    assert settings.raw_dir == ROOT / "data" / "raw"
    assert settings.processed_dir == ROOT / "data" / "processed"
    assert settings.reports_dir == ROOT / "reports"


def test_storage_paths_follow_environment(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "corpus"
    reports_dir = tmp_path / "reports"
    monkeypatch.setenv("VALUECHAIN_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VALUECHAIN_REPORTS_DIR", str(reports_dir))

    settings = Settings()
    ensure_dirs(settings)

    assert settings.data_dir == data_dir
    assert settings.raw_dir == data_dir / "raw"
    assert settings.processed_dir == data_dir / "processed"
    assert settings.reports_dir == reports_dir
    assert settings.raw_dir.is_dir()
    assert settings.processed_dir.is_dir()
    assert settings.reports_dir.is_dir()


def test_specific_storage_paths_override_data_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VALUECHAIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VALUECHAIN_RAW_DIR", str(tmp_path / "raw-objects"))
    monkeypatch.setenv("VALUECHAIN_PROCESSED_DIR", str(tmp_path / "derived"))

    settings = Settings()

    assert settings.raw_dir == tmp_path / "raw-objects"
    assert settings.processed_dir == tmp_path / "derived"
