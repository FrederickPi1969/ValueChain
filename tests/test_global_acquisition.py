from pathlib import Path

import pytest

from gcu.config import Settings
from gcu.http import DownloadedPayload, PoliteHttpClient
from valuechain.global_acquisition import (
    GlobalAcquisitionConfig,
    require_proxy,
    safe_filename,
)


def test_global_acquisition_config_preserves_year_priority(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_GLOBAL_ACQUISITION_YEARS", "2026,2025")

    config = GlobalAcquisitionConfig.from_env()

    assert config.target_years == (2026, 2025)


def test_global_acquisition_caps_async_workers_at_four(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_GLOBAL_CONCURRENCY", "20")

    config = GlobalAcquisitionConfig.from_env()

    assert config.worker_count == 4
    assert config.cninfo_issuer_limit == 16
    assert config.esef_filing_limit == 16


def test_global_acquisition_requires_proxy() -> None:
    settings = Settings(_env_file=None, proxy_pool_url=None)

    with pytest.raises(RuntimeError, match="VALUECHAIN_PROXY_POOL_URL is required"):
        require_proxy(settings)


def test_global_acquisition_accepts_configured_proxy() -> None:
    settings = Settings(_env_file=None, proxy_pool_url="https://proxy.example")

    assert require_proxy(settings) is settings


def test_safe_filename_removes_paths_and_unsafe_characters() -> None:
    assert safe_filename("https://example.test/a/report%20one.pdf?x=1", "fallback") == (
        "report_20one.pdf"
    )
    assert Path(safe_filename("https://example.test/", "fallback.bin")).name == "fallback.bin"


def test_xhtml_is_valid_for_an_expected_html_report(tmp_path: Path) -> None:
    payload = DownloadedPayload(
        temporary_path=tmp_path / "report.xhtml",
        sha256="abc",
        content_length=13,
        media_type="application/xhtml+xml",
        http_status=200,
        final_url="https://example.test/report.xhtml",
        response_headers={},
        first_bytes=b"<html></html>",
    )

    PoliteHttpClient.validate_payload(payload, "text/html", "report.xhtml")


def test_html_is_rejected_when_a_zip_is_expected(tmp_path: Path) -> None:
    payload = DownloadedPayload(
        temporary_path=tmp_path / "report.zip",
        sha256="abc",
        content_length=13,
        media_type="text/html",
        http_status=200,
        final_url="https://example.test/report.zip",
        response_headers={},
        first_bytes=b"<html></html>",
    )

    try:
        PoliteHttpClient.validate_payload(payload, "application/zip", "report.zip")
    except Exception as exc:
        assert "expected application/zip" in str(exc)
    else:
        raise AssertionError("Expected an HTML error page to fail ZIP validation")


def test_sec_json_body_is_accepted_when_content_type_is_text_html(tmp_path: Path) -> None:
    payload = DownloadedPayload(
        temporary_path=tmp_path / "index.json",
        sha256="abc",
        content_length=20,
        media_type="text/html",
        http_status=200,
        final_url="https://www.sec.gov/example/index.json",
        response_headers={},
        first_bytes=b'{"directory":{"item":[]}}',
    )

    PoliteHttpClient.validate_payload(payload, "application/json", "index.json")
