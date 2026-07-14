from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from valuechain.acquisition_api import public_row, require_file_api_access
from valuechain.ad_hoc_state import AdHocRequestState, serialize_request_row
from valuechain.disclosure_resolver import (
    CompanyIdentifier,
    ResolveDisclosureRequest,
    fallback_decision,
    select_local_documents,
)
from valuechain.disclosure_schema import (
    CanonicalDocumentType,
    SOURCE_SCHEMAS,
    get_source_schema,
    source_schema_catalog,
)


router = APIRouter(
    prefix="/api/acquisition",
    tags=["unified-disclosure-resolver"],
    dependencies=[Depends(require_file_api_access)],
)


async def _fetch_all(
    request: Request, query: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    async with request.app.state.pool.connection() as connection:
        cursor = await connection.execute(query, params)
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _fetch_one(
    request: Request, query: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    rows = await _fetch_all(request, query, params)
    return rows[0] if rows else None


@router.get(
    "/schema",
    summary="Inspect unified disclosure schema",
    description=(
        "Returns the cross-market request JSON Schema, canonical document types, "
        "source-native names/codes, company identifiers, credentials, fallback "
        "modes, policy notes, and the resolver execution order."
    ),
    responses={401: {"description": "Missing or invalid API token."}},
)
async def disclosure_schema() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_parameters": ResolveDisclosureRequest.model_json_schema(),
        "canonical_document_types": [item.value for item in CanonicalDocumentType],
        "sources": source_schema_catalog(),
        "resolution_order": [
            "resolve_company_in_source_registry",
            "search_complete_local_documents",
            "queue_legal_on_demand_connector_on_miss",
            "download_original_to_raw_storage",
            "persist_filing_document_hash_and_provenance",
            "return_document_metadata_and_download_url",
        ],
    }


def _candidate_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": row["source_id"],
        "source_issuer_id": row["source_issuer_id"],
        "ticker": row.get("ticker") or "",
        "company_name": row.get("company_name") or "",
        "exchange": row.get("exchange") or "",
    }


async def _resolve_issuer(
    api_request: Request, query: ResolveDisclosureRequest
) -> dict[str, Any]:
    clauses = ["true"]
    params: list[Any] = []
    if query.source_id:
        clauses.append("source_id = %s")
        params.append(query.source_id)

    identifier = CompanyIdentifier(query.company_identifier)
    if identifier == CompanyIdentifier.SOURCE_ISSUER_ID:
        clauses.append("source_issuer_id = %s")
        params.append(query.company)
    elif identifier == CompanyIdentifier.TICKER:
        clauses.append("upper(ticker) = upper(%s)")
        params.append(query.company)
    elif identifier == CompanyIdentifier.LEGAL_NAME:
        clauses.append("company_name ILIKE %s")
        params.append(query.company)
    else:
        clauses.append(
            "(source_issuer_id = %s OR upper(ticker) = upper(%s) "
            "OR company_name ILIKE %s)"
        )
        params.extend([query.company, query.company, query.company])

    rows = await _fetch_all(
        api_request,
        f"""
        SELECT source_id, source_issuer_id, ticker, company_name, exchange,
               priority, metadata,
               CASE
                 WHEN source_issuer_id = %s THEN 0
                 WHEN upper(ticker) = upper(%s) THEN 1
                 WHEN lower(company_name) = lower(%s) THEN 2
                 ELSE 3
               END AS match_rank
        FROM acquisition_issuers
        WHERE {' AND '.join(clauses)}
        ORDER BY match_rank, priority, company_name
        LIMIT 20
        """,
        (query.company, query.company, query.company, *params),
    )
    requested_type = CanonicalDocumentType(query.document_type)
    rows = [
        row
        for row in rows
        if row["source_id"] in SOURCE_SCHEMAS
        and requested_type
        in {mapping.canonical_type for mapping in get_source_schema(row["source_id"]).mappings}
    ]
    if not rows:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "company_not_found",
                "message": "No source issuer matched the company and document type",
            },
        )
    best_rank = int(rows[0]["match_rank"])
    best = [row for row in rows if int(row["match_rank"]) == best_rank]
    if len(best) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ambiguous_company",
                "message": "Specify source_id or company_identifier to select one issuer",
                "candidates": [_candidate_summary(row) for row in best],
            },
        )
    return best[0]


async def _local_candidates(
    api_request: Request,
    source_id: str,
    source_issuer_id: str,
    year: int,
) -> list[dict[str, Any]]:
    rows = await _fetch_all(
        api_request,
        """
        SELECT d.document_id, d.source_id, d.source_filing_id, d.document_kind,
               d.source_url, d.content_type, d.byte_size, d.sha256,
               d.retrieved_at, d.status AS document_status,
               f.source_issuer_id, f.form_raw, f.filing_date, f.report_date,
               f.accepted_at, f.archive_url, f.status AS filing_status,
               f.metadata, i.company_name, i.ticker, i.exchange
        FROM acquisition_filings f
        JOIN acquisition_issuers i
          ON i.source_id = f.source_id AND i.source_issuer_id = f.source_issuer_id
        JOIN acquisition_documents d
          ON d.source_id = f.source_id AND d.source_filing_id = f.source_filing_id
        WHERE f.source_id = %s AND f.source_issuer_id = %s
          AND f.filing_date >= make_date(%s - 1, 1, 1)
          AND f.filing_date < make_date(%s + 2, 1, 1)
          AND f.status = 'complete' AND d.status = 'complete'
        ORDER BY f.filing_date DESC, d.document_id DESC
        """,
        (source_id, source_issuer_id, year, year),
    )
    return rows


def _document_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = public_row(row)
    payload["download_url"] = (
        f"/api/acquisition/documents/{row['document_id']}/download"
    )
    payload["canonical_document_type"] = get_source_schema(
        str(row["source_id"])
    ).canonicalize(str(row["form_raw"])).value
    return payload


async def _documents_by_id(
    api_request: Request, document_ids: list[int]
) -> list[dict[str, Any]]:
    if not document_ids:
        return []
    rows = await _fetch_all(
        api_request,
        """
        SELECT d.document_id, d.source_id, d.source_filing_id, d.document_kind,
               d.source_url, d.content_type, d.byte_size, d.sha256,
               d.retrieved_at, d.status AS document_status,
               f.source_issuer_id, f.form_raw, f.filing_date, f.report_date,
               f.accepted_at, f.archive_url, f.status AS filing_status,
               f.metadata, i.company_name, i.ticker, i.exchange
        FROM acquisition_documents d
        JOIN acquisition_filings f
          ON f.source_id = d.source_id AND f.source_filing_id = d.source_filing_id
        JOIN acquisition_issuers i
          ON i.source_id = f.source_id AND i.source_issuer_id = f.source_issuer_id
        WHERE d.document_id = ANY(%s) AND d.status = 'complete'
        ORDER BY f.filing_date DESC, d.document_id
        """,
        (document_ids,),
    )
    return rows


async def _request_status_payload(
    api_request: Request, request_id: UUID | str
) -> dict[str, Any]:
    row = await _fetch_one(
        api_request,
        "SELECT * FROM acquisition_ad_hoc_requests WHERE request_id = %s",
        (request_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Ad hoc request not found")
    payload = serialize_request_row(row)
    result = row.get("result") or {}
    if isinstance(result, dict) and result.get("retrieval"):
        payload["retrieval"] = result["retrieval"]
    if row["status"] == "complete":
        documents = await _documents_by_id(
            api_request, list(row.get("result_document_ids") or [])
        )
        payload["documents"] = [_document_payload(item) for item in documents]
    payload["status_url"] = f"/api/acquisition/requests/{request_id}"
    return payload


@router.post(
    "/resolve",
    summary="Resolve or acquire a company disclosure",
    description="""
Resolve a company/year/document-type combination against the local corpus first.

* A complete local match returns immediately with `retrieval=local`.
* A miss on SEC, CNINFO, or OpenDART creates or reuses a deduplicated persistent
  job. `wait_seconds` controls synchronous waiting; unfinished work returns 202.
* Scheduled-bulk, current-only, and authorized-import sources return a structured
  capability response and are never silently scraped.
* Periodic reports use report/fiscal year by default. Current and material-event
  reports use filing year. Set `year_basis` to override this behavior.
""",
    responses={
        200: {"description": "Local hit or a completed upstream acquisition."},
        202: {"description": "Persistent job is queued, discovering, downloading, or retrying."},
        401: {"description": "Missing or invalid API token."},
        404: {"description": "Company not found, fallback disabled, or upstream found no matching report."},
        409: {"description": "Ambiguous company or source has no legal real-time fallback lane."},
        422: {"description": "Invalid canonical/native type combination or request parameter."},
    },
)
async def resolve_disclosure(
    query: ResolveDisclosureRequest, request: Request, response: Response
) -> dict[str, Any]:
    issuer = await _resolve_issuer(request, query)
    source_id = str(issuer["source_id"])
    source_issuer_id = str(issuer["source_issuer_id"])
    rows = await _local_candidates(
        request, source_id, source_issuer_id, query.year
    )
    matches = select_local_documents(rows, query)
    if matches:
        return {
            "status": "complete",
            "retrieval": "local",
            "issuer": _candidate_summary(issuer),
            "documents": [_document_payload(row) for row in matches],
        }

    can_queue, reason = fallback_decision(source_id, query.allow_fallback)
    source_schema = get_source_schema(source_id)
    if not can_queue:
        http_status = 404 if reason == "fallback_disabled" else 409
        raise HTTPException(
            status_code=http_status,
            detail={
                "code": "local_miss_no_realtime_fallback",
                "source_id": source_id,
                "fallback_mode": source_schema.fallback_mode.value,
                "message": source_schema.fallback_notes,
            },
        )

    job = await asyncio.to_thread(
        _enqueue,
        request.app.state.database_url,
        query,
        source_id,
        source_issuer_id,
    )
    if query.wait_seconds:
        deadline = asyncio.get_running_loop().time() + query.wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            payload = await _request_status_payload(request, job["request_id"])
            if payload["status"] in {"complete", "not_found", "failed", "unsupported"}:
                return payload
            await asyncio.sleep(0.5)
    payload = await _request_status_payload(request, job["request_id"])
    if payload["status"] not in {"complete", "not_found", "failed", "unsupported"}:
        response.status_code = status.HTTP_202_ACCEPTED
    return payload


def _enqueue(
    database_url: str,
    query: ResolveDisclosureRequest,
    source_id: str,
    source_issuer_id: str,
) -> dict[str, Any]:
    with AdHocRequestState(database_url, ensure_schema=False) as state:
        return state.enqueue(query, source_id, source_issuer_id)


@router.get(
    "/requests/{request_id}",
    summary="Poll an ad hoc disclosure request",
    description=(
        "Returns queue state, attempts, errors, result metadata, and complete "
        "document download URLs. Terminal states are `complete`, `not_found`, "
        "`failed`, and `unsupported`."
    ),
    responses={
        401: {"description": "Missing or invalid API token."},
        404: {"description": "Unknown request id."},
    },
)
async def ad_hoc_request_status(
    request_id: UUID, request: Request
) -> dict[str, Any]:
    return await _request_status_payload(request, request_id)
