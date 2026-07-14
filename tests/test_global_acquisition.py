from pathlib import Path

import pytest

from gcu.config import Settings
from gcu.http import DownloadedPayload, PoliteHttpClient
from valuechain.global_acquisition import (
    GlobalAcquisitionConfig,
    is_report_summary,
    require_proxy,
    safe_filename,
)
from valuechain.global_acquisition_state import (
    FILING_CLAIM_ORDER_SQL,
    SOURCE_OBJECT_CLAIM_ORDER_SQL,
    filing_local_dir,
)


def test_global_state_has_atomic_single_filing_claim_for_ad_hoc_work() -> None:
    from valuechain.global_acquisition_state import GlobalSourceAcquisitionState

    assert callable(GlobalSourceAcquisitionState.claim_filing)


def test_global_acquisition_config_preserves_year_priority(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_GLOBAL_ACQUISITION_YEARS", "2026,2025")

    config = GlobalAcquisitionConfig.from_env()

    assert config.target_years == (2026, 2025)


def test_global_filing_queue_claims_newest_filings_first() -> None:
    assert FILING_CLAIM_ORDER_SQL == "filing_date DESC, source_filing_id DESC"


def test_bulk_object_queue_claims_newest_effective_date_first() -> None:
    assert "effective_date" in SOURCE_OBJECT_CLAIM_ORDER_SQL
    assert "DESC" in SOURCE_OBJECT_CLAIM_ORDER_SQL


def test_global_acquisition_caps_async_workers_at_four(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_GLOBAL_CONCURRENCY", "20")

    config = GlobalAcquisitionConfig.from_env()

    assert config.worker_count == 4
    assert config.cninfo_issuer_limit == 16
    assert config.esef_filing_limit == 16
    assert config.cninfo_rescan_hours == 24


def test_opendart_runtime_limits_cannot_exceed_safe_caps(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_OPENDART_DAILY_REQUEST_BUDGET", "999999")
    monkeypatch.setenv("VALUECHAIN_OPENDART_REQUESTS_PER_SECOND", "20")
    monkeypatch.setenv("VALUECHAIN_OPENDART_CONCURRENCY", "20")

    config = GlobalAcquisitionConfig.from_env()

    assert config.opendart_daily_request_budget == 10_000
    assert config.opendart_requests_per_second == 1.0
    assert config.opendart_worker_count == 2


def test_edinet_runtime_limits_cannot_exceed_safe_caps(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_EDINET_DAILY_REQUEST_BUDGET", "999999")
    monkeypatch.setenv("VALUECHAIN_EDINET_REQUESTS_PER_SECOND", "20")
    monkeypatch.setenv("VALUECHAIN_EDINET_CONCURRENCY", "20")

    config = GlobalAcquisitionConfig.from_env()

    assert config.edinet_daily_request_budget == 1_000
    assert config.edinet_requests_per_second == 1.0
    assert config.edinet_worker_count == 2


def test_new_public_source_runtime_rates_are_conservatively_capped(monkeypatch) -> None:
    monkeypatch.setenv("VALUECHAIN_TAIWAN_REQUESTS_PER_SECOND", "20")
    monkeypatch.setenv("VALUECHAIN_COMPANIES_HOUSE_BULK_REQUESTS_PER_SECOND", "20")
    monkeypatch.setenv("VALUECHAIN_CVM_REQUESTS_PER_SECOND", "20")

    config = GlobalAcquisitionConfig.from_env()

    assert config.taiwan_requests_per_second == 1.0
    assert config.companies_house_bulk_requests_per_second == 0.5
    assert config.cvm_requests_per_second == 1.0


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


def test_report_summary_filter_handles_chinese_and_english() -> None:
    assert is_report_summary("2025年年度报告摘要")
    assert is_report_summary("Summary of 2025 Annual Report")
    assert is_report_summary("Abstract of the Semi-Annual Report 2025")
    assert not is_report_summary("2025 Annual Report")


def test_cninfo_filing_directory_includes_issuer_partition(tmp_path: Path) -> None:
    assert filing_local_dir(tmp_path, "cninfo", 2026, "issuer/1", "filing/2") == (
        tmp_path / "cninfo" / "2026" / "issuer_1" / "filing_2"
    )


def test_esef_filing_directory_uses_filing_partition(tmp_path: Path) -> None:
    assert filing_local_dir(
        tmp_path, "priority_eu_esef", 2026, "issuer", "filing/2"
    ) == (tmp_path / "priority_eu_esef" / "2026" / "filing_2")


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


def test_zip_signature_allows_octet_stream_content_type(tmp_path: Path) -> None:
    payload = DownloadedPayload(
        temporary_path=tmp_path / "filing.zip",
        sha256="abc",
        content_length=11,
        media_type="application/octet-stream",
        http_status=200,
        final_url="https://example.test/document.xml",
        response_headers={},
        first_bytes=b"PK\x03\x04payload",
    )

    PoliteHttpClient.validate_payload(payload, "application/zip", "filing.zip")


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
