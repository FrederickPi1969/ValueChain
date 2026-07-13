from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gcu_priority_markets.catalog import load_contracts


DEFAULT_PATTERNS = {
    "lse_issuer_list": [r"issuer", r"report", r"xlsx", r"csv", r"zip"],
    "fca_official_list": [r"official", r"list", r"issuer", r"security", r"csv"],
    "fca_nsm": [r"nsm", r"nationalstoragemechanism", r"document"],
    "sedar_plus": [r"searchDocuments", r"document", r"profile"],
    "six_exchange": [r"shares", r"issuer", r"news", r"csv"],
    "bmv": [r"emisora", r"eventos", r"informacion", r"archivo"],
    "idx_indonesia": [r"listed", r"announcement", r"financial", r"report"],
    "tadawul": [r"issuer", r"announcement", r"financial", r"calendar"],
    "sgx": [r"announcement", r"securities", r"sgxnet", r"company"],
    "unternehmensregister": [r"publication", r"document", r"register"],
    "amf_france": [r"bdif", r"document", r"issuer"],
    "consob": [r"emittenti", r"document", r"informazione"],
    "cnmv": [r"consulta", r"xbrl", r"document"],
    "afm_netherlands": [r"register", r"verslag", r"document"],
}


def _safe_name(url: str, media_type: str | None, index: int) -> str:
    path = urlparse(url).path
    name = Path(path).name
    if not name or "." not in name:
        suffix = mimetypes.guess_extension((media_type or "").split(";", 1)[0]) or ".bin"
        name = f"response-{index:04d}{suffix}"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:180]
    return name or f"response-{index:04d}.bin"


def capture_official_session(
    *,
    source_id: str,
    output_dir: Path,
    headless: bool = True,
    wait_seconds: float = 15.0,
    page_url: str | None = None,
    patterns: list[str] | None = None,
    click_texts: list[str] | None = None,
    click_selectors: list[str] | None = None,
) -> dict[str, Any]:
    """Capture machine-readable responses from a user-controlled official-page session.

    This does not solve CAPTCHAs, bypass authentication, persist credentials, or evade
    source restrictions. It is an operator-side observability aid for official exports.
    """

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional installation
        raise RuntimeError(
            "Playwright is optional. Install with `pip install gcu-priority-markets-patch[browser]` "
            "and run `playwright install chromium`."
        ) from exc

    contracts = load_contracts()
    contract = contracts.get(source_id, {})
    target_url = page_url or contract.get("authority_page")
    if not target_url:
        raise ValueError(f"No authority page configured for source {source_id!r}")
    regexes = [re.compile(item, re.I) for item in (patterns or DEFAULT_PATTERNS.get(source_id, []))]
    if not regexes:
        raise ValueError(f"No capture patterns configured for source {source_id!r}")

    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    downloads: list[dict[str, Any]] = []
    with sync_playwright() as playwright:  # pragma: no cover - browser integration
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        def on_response(response: Any) -> None:
            url = response.url
            if not any(regex.search(url) for regex in regexes):
                return
            media_type = response.headers.get("content-type")
            likely_data = any(
                marker in (media_type or "").lower()
                for marker in (
                    "json",
                    "csv",
                    "xml",
                    "excel",
                    "spreadsheet",
                    "pdf",
                    "zip",
                    "octet-stream",
                )
            )
            if not likely_data and response.request.resource_type not in {"xhr", "fetch", "document"}:
                return
            try:
                content = response.body()
            except Exception:  # noqa: BLE001
                return
            if not content:
                return
            digest = hashlib.sha256(content).hexdigest()
            name = _safe_name(url, media_type, len(records) + 1)
            destination = output_dir / f"{digest[:12]}-{name}"
            destination.write_bytes(content)
            records.append(
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "source_id": source_id,
                    "url": url,
                    "status": response.status,
                    "media_type": media_type,
                    "resource_type": response.request.resource_type,
                    "bytes": len(content),
                    "sha256": digest,
                    "local_path": str(destination),
                }
            )

        def on_download(download: Any) -> None:
            try:
                suggested = re.sub(r"[^A-Za-z0-9._-]+", "_", download.suggested_filename)[:180]
                source_path = download.path()
                if source_path is None:
                    return
                payload = Path(source_path).read_bytes()
                digest = hashlib.sha256(payload).hexdigest()
                destination = output_dir / f"{digest[:12]}-{suggested or 'download.bin'}"
                shutil.copyfile(source_path, destination)
                downloads.append(
                    {
                        "captured_at": datetime.now(UTC).isoformat(),
                        "source_id": source_id,
                        "url": download.url,
                        "suggested_filename": download.suggested_filename,
                        "bytes": len(payload),
                        "sha256": digest,
                        "local_path": str(destination),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                downloads.append(
                    {
                        "captured_at": datetime.now(UTC).isoformat(),
                        "source_id": source_id,
                        "url": getattr(download, "url", None),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        page.on("response", on_response)
        page.on("download", on_download)
        page.goto(target_url, wait_until="domcontentloaded")
        for selector in click_selectors or []:
            try:
                page.locator(selector).first.click(timeout=5000)
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001
                continue
        for text in click_texts or []:
            try:
                page.get_by_text(text, exact=False).first.click(timeout=5000)
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001
                continue
        page.wait_for_timeout(int(wait_seconds * 1000))
        (output_dir / "official-page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(output_dir / "official-page.png"), full_page=True)
        context.close()
        browser.close()

    manifest = {
        "source_id": source_id,
        "authority_page": target_url,
        "captured_at": datetime.now(UTC).isoformat(),
        "headless": headless,
        "patterns": [regex.pattern for regex in regexes],
        "response_count": len(records),
        "responses": records,
        "download_count": len(downloads),
        "downloads": downloads,
        "click_texts": click_texts or [],
        "click_selectors": click_selectors or [],
        "policy_boundary": (
            "Operator-controlled official page session only; no CAPTCHA bypass, credential capture, "
            "anti-bot evasion, or licensed-feed redistribution."
        ),
    }
    (output_dir / "capture-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
