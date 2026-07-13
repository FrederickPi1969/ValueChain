from pathlib import Path

from valuechain.global_acquisition import GlobalAcquisitionConfig, safe_filename


def test_global_acquisition_config_preserves_year_priority(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_GLOBAL_ACQUISITION_YEARS", "2026,2025")

    config = GlobalAcquisitionConfig.from_env()

    assert config.target_years == (2026, 2025)


def test_safe_filename_removes_paths_and_unsafe_characters() -> None:
    assert safe_filename("https://example.test/a/report%20one.pdf?x=1", "fallback") == (
        "report_20one.pdf"
    )
    assert Path(safe_filename("https://example.test/", "fallback.bin")).name == "fallback.bin"
