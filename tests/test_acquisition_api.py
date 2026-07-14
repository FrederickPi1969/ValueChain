from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import valuechain.acquisition_api as acquisition_api
from valuechain.acquisition_api import (
    add_canonical_document_type,
    download_response,
    public_row,
    resolve_download_path,
    router,
)


def test_legacy_file_api_rows_include_canonical_document_type() -> None:
    assert add_canonical_document_type(
        {"source_id": "sec_edgar", "form_raw": "20-F"}
    )["canonical_document_type"] == "annual_report"
    assert add_canonical_document_type(
        {"source_id": "unknown", "form_raw": "x"}
    )["canonical_document_type"] == "other_regulatory_filing"


def build_test_app(root: Path, *, token: str = "") -> FastAPI:
    app = FastAPI()
    app.state.acquisition_file_roots = (root,)
    app.state.file_api_token = token
    app.include_router(router)
    return app


def test_public_row_hides_server_paths_and_errors() -> None:
    assert public_row(
        {"document_id": 7, "local_path": "/secret/file", "last_error": "x"}
    ) == {"document_id": 7}


def test_resolve_download_path_enforces_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    document = allowed / "filing.txt"
    document.write_text("evidence")

    assert resolve_download_path(str(document), (allowed,)) == document.resolve()

    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    with pytest.raises(HTTPException) as error:
        resolve_download_path(str(outside), (allowed,))
    assert error.value.status_code == 403


def test_resolve_download_path_rejects_symlink_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    link = allowed / "link.txt"
    link.symlink_to(outside)

    with pytest.raises(HTTPException) as error:
        resolve_download_path(str(link), (allowed,))
    assert error.value.status_code == 403


def test_download_response_sets_checksum_headers(tmp_path: Path) -> None:
    document = tmp_path / "filing.txt"
    document.write_text("evidence")

    response = download_response(
        {
            "local_path": str(document),
            "content_type": "text/plain",
            "sha256": "abc123",
        },
        (tmp_path,),
    )

    assert response.headers["etag"] == '"abc123"'
    assert response.headers["x-checksum-sha256"] == "abc123"
    assert response.headers["accept-ranges"] == "bytes"


def test_document_download_supports_http_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = tmp_path / "filing.txt"
    document.write_bytes(b"0123456789")

    async def fake_fetch_one(*_args, **_kwargs):
        return {
            "document_id": 7,
            "local_path": str(document),
            "content_type": "text/plain",
            "byte_size": 10,
            "sha256": "abc123",
            "status": "complete",
        }

    monkeypatch.setattr(acquisition_api, "_fetch_one", fake_fetch_one)
    client = TestClient(build_test_app(tmp_path))

    response = client.get(
        "/api/acquisition/documents/7/download",
        headers={"Range": "bytes=2-5"},
    )

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["etag"] == '"abc123"'


def test_file_api_token_protects_acquisition_routes(tmp_path: Path) -> None:
    client = TestClient(build_test_app(tmp_path, token="secret-token"))

    assert client.get("/api/acquisition/sources").status_code == 401


def test_sources_endpoint_returns_serializable_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch_all(*_args, **_kwargs):
        return [{"source_id": "sec_edgar", "documents": 12}]

    monkeypatch.setattr(acquisition_api, "_fetch_all", fake_fetch_all)
    client = TestClient(build_test_app(tmp_path, token="secret-token"))

    response = client.get(
        "/api/acquisition/sources", headers={"X-API-Key": "secret-token"}
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["source_id"] == "sec_edgar"
