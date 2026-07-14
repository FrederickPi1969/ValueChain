from __future__ import annotations

import secrets
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from valuechain.disclosure_schema import canonicalize_document_type


router = APIRouter(prefix="/api/acquisition", tags=["acquisition-files"])

api_key_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="AcquisitionApiKey",
    description="ValueChain file API token sent as `X-API-Key`.",
    auto_error=False,
)
bearer_header = HTTPBearer(
    scheme_name="AcquisitionBearer",
    description="The same ValueChain file API token sent as a Bearer token.",
    auto_error=False,
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _json_value(value)
        for key, value in row.items()
        if key not in {"local_path", "last_error"}
    }


def page(items: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": [public_row(item) for item in items],
        "limit": limit,
        "offset": offset,
        "has_more": len(items) == limit,
    }


def add_canonical_document_type(row: dict[str, Any]) -> dict[str, Any]:
    source_id = str(row.get("source_id") or "")
    form_raw = str(row.get("form_raw") or "")
    try:
        row["canonical_document_type"] = canonicalize_document_type(
            source_id, form_raw
        ).value
    except ValueError:
        row["canonical_document_type"] = "other_regulatory_filing"
    return row


async def require_file_api_access(
    request: Request,
    x_api_key: str | None = Security(api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(bearer_header),
) -> None:
    expected = str(getattr(request.app.state, "file_api_token", "") or "")
    if not expected:
        return
    supplied = x_api_key or (bearer.credentials if bearer else "")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="A valid file API token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _fetch_all(
    request: Request, query: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    async with request.app.state.pool.connection() as conn:
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _fetch_one(
    request: Request, query: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    async with request.app.state.pool.connection() as conn:
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()
    return dict(row) if row else None


def resolve_download_path(value: str, allowed_roots: tuple[Path, ...]) -> Path:
    if not value:
        raise HTTPException(status_code=404, detail="File path is not recorded")
    try:
        path = Path(value).resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=404, detail="Recorded file is missing") from None
    roots: list[Path] = []
    for root in allowed_roots:
        try:
            roots.append(root.resolve(strict=True))
        except (FileNotFoundError, OSError):
            continue
    if not any(path.is_relative_to(root) for root in roots):
        raise HTTPException(status_code=403, detail="Recorded path is outside allowed roots")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Recorded path is not a file")
    return path


def download_response(
    row: dict[str, Any], allowed_roots: tuple[Path, ...]
) -> FileResponse:
    path = resolve_download_path(str(row.get("local_path") or ""), allowed_roots)
    sha256 = str(row.get("sha256") or "").strip()
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    }
    if sha256:
        headers["ETag"] = f'"{sha256}"'
        headers["X-Checksum-SHA256"] = sha256
    return FileResponse(
        path,
        filename=path.name,
        media_type=str(row.get("content_type") or "application/octet-stream"),
        headers=headers,
    )


@router.get(
    "/sources",
    summary="List acquisition source coverage",
    dependencies=[Depends(require_file_api_access)],
)
async def acquisition_sources(request: Request) -> dict[str, Any]:
    """List configured sources with issuer, filing, document, object, and byte totals."""
    rows = await _fetch_all(
        request,
        """
        WITH issuer_counts AS (
          SELECT source_id, count(*)::bigint AS issuers
          FROM acquisition_issuers GROUP BY source_id
        ), filing_counts AS (
          SELECT source_id, count(*)::bigint AS filings,
                 count(*) FILTER (WHERE status = 'complete')::bigint AS complete_filings
          FROM acquisition_filings GROUP BY source_id
        ), document_counts AS (
          SELECT source_id,
                 count(*) FILTER (WHERE status = 'complete')::bigint AS documents,
                 coalesce(sum(byte_size) FILTER (WHERE status = 'complete'), 0)::bigint AS document_bytes
          FROM acquisition_documents GROUP BY source_id
        ), object_counts AS (
          SELECT source_id,
                 count(*) FILTER (WHERE status = 'complete')::bigint AS source_objects,
                 coalesce(sum(byte_size) FILTER (WHERE status = 'complete'), 0)::bigint AS source_object_bytes
          FROM acquisition_source_objects GROUP BY source_id
        )
        SELECT s.source_id, s.authority, s.canonical, s.enabled, s.updated_at,
               coalesce(i.issuers, 0) AS issuers,
               coalesce(f.filings, 0) AS filings,
               coalesce(f.complete_filings, 0) AS complete_filings,
               coalesce(d.documents, 0) AS documents,
               coalesce(d.document_bytes, 0) AS document_bytes,
               coalesce(o.source_objects, 0) AS source_objects,
               coalesce(o.source_object_bytes, 0) AS source_object_bytes
        FROM acquisition_sources s
        LEFT JOIN issuer_counts i USING (source_id)
        LEFT JOIN filing_counts f USING (source_id)
        LEFT JOIN document_counts d USING (source_id)
        LEFT JOIN object_counts o USING (source_id)
        ORDER BY s.source_id
        """,
    )
    return {"items": [public_row(row) for row in rows]}


@router.get(
    "/issuers",
    summary="Search source issuer registry",
    dependencies=[Depends(require_file_api_access)],
)
async def acquisition_issuers(
    request: Request,
    source_id: str = "",
    q: str = "",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Search the source-local issuer registry by name, ticker, or native issuer id."""
    clauses = ["true"]
    params: list[Any] = []
    if source_id:
        clauses.append("i.source_id = %s")
        params.append(source_id)
    if q:
        clauses.append(
            "(i.company_name ILIKE %s OR i.ticker ILIKE %s OR i.source_issuer_id ILIKE %s)"
        )
        params.extend([f"%{q}%"] * 3)
    rows = await _fetch_all(
        request,
        f"""
        SELECT i.source_id, i.source_issuer_id, i.ticker, i.company_name,
               i.exchange, i.priority, i.created_at, i.updated_at,
               count(f.source_filing_id)::bigint AS filing_count,
               max(f.filing_date) AS latest_filing_date
        FROM acquisition_issuers i
        LEFT JOIN acquisition_filings f
          ON f.source_id = i.source_id AND f.source_issuer_id = i.source_issuer_id
        WHERE {' AND '.join(clauses)}
        GROUP BY i.source_id, i.source_issuer_id
        ORDER BY latest_filing_date DESC NULLS LAST, i.priority, i.company_name
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    return page(rows, limit, offset)


@router.get(
    "/filings",
    summary="Search normalized filing inventory",
    dependencies=[Depends(require_file_api_access)],
)
async def acquisition_filings(
    request: Request,
    source_id: str = "",
    issuer_id: str = "",
    form: str = "",
    status: str = "",
    year: int | None = Query(default=None, ge=1990, le=2100),
    q: str = "",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List filings while preserving both `form_raw` and `canonical_document_type`."""
    clauses = ["true"]
    params: list[Any] = []
    filters = (
        (source_id, "f.source_id = %s"),
        (issuer_id, "f.source_issuer_id = %s"),
        (form, "f.form_raw = %s"),
        (status, "f.status = %s"),
    )
    for value, clause in filters:
        if value:
            clauses.append(clause)
            params.append(value)
    if year is not None:
        clauses.append("extract(year FROM f.filing_date) = %s")
        params.append(year)
    if q:
        clauses.append(
            "(i.company_name ILIKE %s OR i.ticker ILIKE %s OR "
            "f.source_filing_id ILIKE %s OR f.form_raw ILIKE %s)"
        )
        params.extend([f"%{q}%"] * 4)
    rows = await _fetch_all(
        request,
        f"""
        SELECT f.source_id, f.source_filing_id, f.source_issuer_id,
               i.company_name, i.ticker, i.exchange, f.form_raw,
               f.filing_date, f.report_date, f.accepted_at, f.archive_url,
               f.status, f.discovered_at,
               count(d.document_id)::bigint AS document_count,
               coalesce(sum(d.byte_size) FILTER (WHERE d.status = 'complete'), 0)::bigint AS document_bytes
        FROM acquisition_filings f
        JOIN acquisition_issuers i
          ON i.source_id = f.source_id AND i.source_issuer_id = f.source_issuer_id
        LEFT JOIN acquisition_documents d
          ON d.source_id = f.source_id AND d.source_filing_id = f.source_filing_id
        WHERE {' AND '.join(clauses)}
        GROUP BY f.source_id, f.source_filing_id, i.source_id, i.source_issuer_id
        ORDER BY f.filing_date DESC, f.source_id, f.source_filing_id
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    for row in rows:
        add_canonical_document_type(row)
        row["detail_url"] = (
            f"/api/acquisition/filings/{quote(str(row['source_id']), safe='')}/"
            f"{quote(str(row['source_filing_id']), safe='')}"
        )
    return page(rows, limit, offset)


@router.get(
    "/filings/{source_id}/{filing_id:path}",
    summary="Inspect one filing and its documents",
    dependencies=[Depends(require_file_api_access)],
)
async def acquisition_filing_detail(
    source_id: str, filing_id: str, request: Request
) -> dict[str, Any]:
    """Return one filing, its provenance metadata, and all associated documents."""
    filing = await _fetch_one(
        request,
        """
        SELECT f.source_id, f.source_filing_id, f.source_issuer_id,
               i.company_name, i.ticker, i.exchange, f.form_raw,
               f.filing_date, f.report_date, f.accepted_at, f.archive_url,
               f.status, f.discovered_at, f.metadata
        FROM acquisition_filings f
        JOIN acquisition_issuers i
          ON i.source_id = f.source_id AND i.source_issuer_id = f.source_issuer_id
        WHERE f.source_id = %s AND f.source_filing_id = %s
        """,
        (source_id, filing_id),
    )
    if not filing:
        raise HTTPException(status_code=404, detail="Filing not found")
    add_canonical_document_type(filing)
    documents = await _fetch_all(
        request,
        """
        SELECT document_id, source_id, source_filing_id, document_kind,
               source_url, content_type, byte_size, sha256, retrieved_at,
               status, metadata
        FROM acquisition_documents
        WHERE source_id = %s AND source_filing_id = %s
        ORDER BY document_kind, document_id
        """,
        (source_id, filing_id),
    )
    for document in documents:
        if document.get("status") == "complete":
            document["download_url"] = (
                f"/api/acquisition/documents/{document['document_id']}/download"
            )
    return {
        "filing": public_row(filing),
        "documents": [public_row(row) for row in documents],
    }


@router.get(
    "/documents",
    summary="Search stored disclosure documents",
    dependencies=[Depends(require_file_api_access)],
)
async def acquisition_documents(
    request: Request,
    source_id: str = "",
    filing_id: str = "",
    status: str = "complete",
    sha256: str = "",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Search complete acquisition documents and obtain authenticated download URLs."""
    clauses = ["true"]
    params: list[Any] = []
    for value, clause in (
        (source_id, "d.source_id = %s"),
        (filing_id, "d.source_filing_id = %s"),
        (status, "d.status = %s"),
        (sha256, "d.sha256 = %s"),
    ):
        if value:
            clauses.append(clause)
            params.append(value)
    rows = await _fetch_all(
        request,
        f"""
        SELECT d.document_id, d.source_id, d.source_filing_id, d.document_kind,
               f.source_issuer_id, i.company_name, i.ticker, f.form_raw,
               f.filing_date, d.source_url, d.content_type, d.byte_size,
               d.sha256, d.retrieved_at, d.status, d.metadata
        FROM acquisition_documents d
        JOIN acquisition_filings f
          ON f.source_id = d.source_id AND f.source_filing_id = d.source_filing_id
        JOIN acquisition_issuers i
          ON i.source_id = f.source_id AND i.source_issuer_id = f.source_issuer_id
        WHERE {' AND '.join(clauses)}
        ORDER BY f.filing_date DESC, d.document_id DESC
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    for row in rows:
        add_canonical_document_type(row)
        if row.get("status") == "complete":
            row["download_url"] = (
                f"/api/acquisition/documents/{row['document_id']}/download"
            )
    return page(rows, limit, offset)


@router.get(
    "/snapshots",
    summary="List issuer-universe snapshots",
    dependencies=[Depends(require_file_api_access)],
)
async def acquisition_universe_snapshots(
    request: Request,
    source_id: str = "",
    status: str = "complete",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List versioned issuer-universe snapshots and their content hashes."""
    clauses = ["true"]
    params: list[Any] = []
    for value, clause in (
        (source_id, "source_id = %s"),
        (status, "status = %s"),
    ):
        if value:
            clauses.append(clause)
            params.append(value)
    rows = await _fetch_all(
        request,
        f"""
        SELECT snapshot_id, source_id, source_url, sha256, row_count,
               retrieved_at, imported_at, status, metadata
        FROM acquisition_universe_snapshots
        WHERE {' AND '.join(clauses)}
        ORDER BY retrieved_at DESC NULLS LAST, snapshot_id DESC
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    for row in rows:
        if row.get("status") == "complete":
            row["download_url"] = (
                f"/api/acquisition/snapshots/{row['snapshot_id']}/download"
            )
    return page(rows, limit, offset)


async def _snapshot_download_response(
    snapshot_id: int, request: Request
) -> FileResponse:
    row = await _fetch_one(
        request,
        """
        SELECT snapshot_id, local_path, '' AS content_type, sha256, status
        FROM acquisition_universe_snapshots WHERE snapshot_id = %s
        """,
        (snapshot_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Universe snapshot not found")
    if row.get("status") != "complete":
        raise HTTPException(status_code=409, detail="Universe snapshot is not complete")
    return download_response(row, request.app.state.acquisition_file_roots)


@router.get(
    "/snapshots/{snapshot_id}/download",
    summary="Download an issuer-universe snapshot",
    dependencies=[Depends(require_file_api_access)],
)
async def download_acquisition_snapshot(
    snapshot_id: int, request: Request
) -> FileResponse:
    """Stream a universe snapshot with ETag, SHA-256, and HTTP byte-range support."""
    return await _snapshot_download_response(snapshot_id, request)


@router.head(
    "/snapshots/{snapshot_id}/download",
    dependencies=[Depends(require_file_api_access)],
    include_in_schema=False,
)
async def head_acquisition_snapshot(
    snapshot_id: int, request: Request
) -> FileResponse:
    return await _snapshot_download_response(snapshot_id, request)


async def _document_download_response(
    document_id: int, request: Request
) -> FileResponse:
    row = await _fetch_one(
        request,
        """
        SELECT document_id, local_path, content_type, byte_size, sha256, status
        FROM acquisition_documents WHERE document_id = %s
        """,
        (document_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    if row.get("status") != "complete":
        raise HTTPException(status_code=409, detail="Document is not complete")
    return download_response(row, request.app.state.acquisition_file_roots)


@router.get(
    "/documents/{document_id}/download",
    summary="Download an original disclosure document",
    dependencies=[Depends(require_file_api_access)],
)
async def download_acquisition_document(
    document_id: int, request: Request
) -> FileResponse:
    """Stream an original disclosure document with byte-range and checksum headers."""
    return await _document_download_response(document_id, request)


@router.head(
    "/documents/{document_id}/download",
    dependencies=[Depends(require_file_api_access)],
    include_in_schema=False,
)
async def head_acquisition_document(
    document_id: int, request: Request
) -> FileResponse:
    return await _document_download_response(document_id, request)


@router.get(
    "/objects",
    summary="List stored bulk source objects",
    dependencies=[Depends(require_file_api_access)],
)
async def acquisition_objects(
    request: Request,
    source_id: str = "",
    object_type: str = "",
    status: str = "complete",
    year: int | None = Query(default=None, ge=1990, le=2100),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List source-level bulk objects such as CVM, Companies House, or authorized packages."""
    clauses = ["true"]
    params: list[Any] = []
    for value, clause in (
        (source_id, "source_id = %s"),
        (object_type, "object_type = %s"),
        (status, "status = %s"),
    ):
        if value:
            clauses.append(clause)
            params.append(value)
    if year is not None:
        clauses.append("metadata->>'filing_year' = %s")
        params.append(str(year))
    rows = await _fetch_all(
        request,
        f"""
        SELECT source_id, object_key, object_type, source_url, content_type,
               byte_size, sha256, retrieved_at, status, metadata
        FROM acquisition_source_objects
        WHERE {' AND '.join(clauses)}
        ORDER BY coalesce(
                   CASE WHEN metadata->>'effective_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                        THEN (metadata->>'effective_date')::date END,
                   DATE '1900-01-01'
                 ) DESC,
                 source_id, object_key DESC
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    for row in rows:
        if row.get("status") == "complete":
            row["download_url"] = (
                f"/api/acquisition/objects/{quote(str(row['source_id']), safe='')}/"
                f"{quote(str(row['object_key']), safe='')}/download"
            )
    return page(rows, limit, offset)


async def _object_download_response(
    source_id: str, object_key: str, request: Request
) -> FileResponse:
    row = await _fetch_one(
        request,
        """
        SELECT source_id, object_key, local_path, content_type, byte_size,
               sha256, status
        FROM acquisition_source_objects
        WHERE source_id = %s AND object_key = %s
        """,
        (source_id, object_key),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Source object not found")
    if row.get("status") != "complete":
        raise HTTPException(status_code=409, detail="Source object is not complete")
    return download_response(row, request.app.state.acquisition_file_roots)


@router.get(
    "/objects/{source_id}/{object_key:path}/download",
    summary="Download a bulk source object",
    dependencies=[Depends(require_file_api_access)],
)
async def download_acquisition_object(
    source_id: str, object_key: str, request: Request
) -> FileResponse:
    """Stream a source-level bulk object with byte-range and checksum headers."""
    return await _object_download_response(source_id, object_key, request)


@router.head(
    "/objects/{source_id}/{object_key:path}/download",
    dependencies=[Depends(require_file_api_access)],
    include_in_schema=False,
)
async def head_acquisition_object(
    source_id: str, object_key: str, request: Request
) -> FileResponse:
    return await _object_download_response(source_id, object_key, request)
