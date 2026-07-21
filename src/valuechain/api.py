from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from valuechain.config import Settings
from valuechain.acquisition_api import router as acquisition_router
from valuechain.acquisition_resolver_api import router as acquisition_resolver_router
from valuechain.acquisition_schema import prepare_acquisition_schema
from valuechain.dashboard import build_dashboard_data
from valuechain.models import Company, GraphEdge, RelationEvidence, SourceDocument
from valuechain.universe_policy_api import router as universe_policy_router


settings = Settings()
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"

API_DESCRIPTION = """
## Disclosure-derived AI value-chain data service

This API serves the evidence corpus, source inventory, extracted dependency
data, and a **local-first unified disclosure resolver**.

### Company coverage

The long-term target is the complete SEC issuer universe, the complete mainland
China CNINFO issuer universe, and a versioned **Global Strategic 1000**. The
strategic universe is counted by issuer group rather than ticker and covers
technology, energy, power, transportation, industrial equipment, materials,
financial infrastructure, healthcare, telecommunications, food systems,
defense, and physical infrastructure. Call
`GET /api/acquisition/universe-policy` for regional and sector quotas, scoring,
mandatory overrides, monitoring tiers, update cadence, retention, and storage
assumptions.

### Recommended disclosure workflow

1. Call `GET /api/acquisition/schema` to inspect canonical document types,
   source-native form names/codes, identifiers, credentials, and fallback modes.
2. Call `POST /api/acquisition/resolve` with a company, year, and canonical
   document type.
3. A local hit returns `200` immediately. A legal on-demand miss is persisted
   and returns `202` when it outlives `wait_seconds`.
4. Poll `GET /api/acquisition/requests/{request_id}` until `complete`, then use
   the returned `download_url`.

### Monitoring and universe updates

New-filing discovery runs at the fastest lawful source-specific cadence. Issuer
registries refresh daily where machine endpoints support it. The Global
Strategic 1000 is rebalanced quarterly, corporate actions are reconciled as
events arrive, and the methodology is reviewed annually. Tier S retains all
material disclosures, Tier A retains periodic and selected material reports,
and Tier B retains annual/interim reports while leaving other documents on
demand. Historical files are never deleted merely because an issuer exits the
current universe.

### Authentication

Acquisition metadata and file routes accept either `X-API-Key` or
`Authorization: Bearer <token>`. The service can be configured without a token
for trusted local development; the Cosmos deployment requires one.

### Storage and provenance

Files are streamed from read-only HDD mounts with byte-range support. Responses
retain source id, source issuer/filing id, native form, canonical type, filing
and report dates, source URL, archive URL, SHA-256, byte size, and retrieval time.

### Source-policy boundary

Only SEC, CNINFO, and OpenDART currently advertise synchronous `on_demand`
fallback. Scheduled bulk and authorized-import sources return an explicit
capability response rather than scraping restricted websites.
"""

OPENAPI_TAGS = [
    {
        "name": "unified-disclosure-resolver",
        "description": "Local-first company/year/report resolution with persistent ad hoc fallback jobs.",
    },
    {
        "name": "acquisition-files",
        "description": "Source, issuer, filing, document, snapshot, object, and byte-range download APIs.",
    },
    {
        "name": "universe-policy",
        "description": "Company coverage, strategic selection, monitoring tiers, refresh cadence, deduplication, and retention policy.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(prepare_acquisition_schema, settings.database_url)
    pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=8,
        open=False,
    )
    await pool.open()
    app.state.pool = pool
    app.state.database_url = settings.database_url
    app.state.acquisition_file_roots = settings.acquisition_file_roots
    app.state.file_api_token = settings.file_api_token
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(
    title="AI Value Chain API",
    summary="Unified global disclosure acquisition and evidence API",
    description=API_DESCRIPTION,
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(acquisition_router)
app.include_router(acquisition_resolver_router)
app.include_router(universe_policy_router)

if (FRONTEND_DIST / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(FRONTEND_DIST / "assets")),
        name="frontend-assets",
    )


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        row = await conn.execute("SELECT 1 AS ok")
        result = await row.fetchone()
    return {"ok": result["ok"] == 1}


@app.get("/", include_in_schema=False)
async def frontend_index() -> FileResponse:
    if not FRONTEND_INDEX.is_file():
        raise HTTPException(status_code=404, detail="Frontend build is not available")
    return FileResponse(FRONTEND_INDEX)


@app.get("/api/runs")
async def list_runs(request: Request) -> dict[str, list[dict[str, Any]]]:
    rows = await fetch_all(
        request,
        """
        SELECT run_id, run_label, created_at, options, counts
        FROM runs
        ORDER BY created_at DESC
        """,
    )
    return {
        "runs": [
            {
                **row,
                "created_at": row["created_at"].isoformat(),
                "data_path": f"/api/runs/{row['run_id']}/dashboard-data",
            }
            for row in rows
        ]
    }


@app.get("/api/runs/{run_id}/companies")
async def companies(run_id: str, request: Request) -> dict[str, Any]:
    rows = await fetch_all(
        request,
        """
        SELECT ticker, company_name, role, priority, notes, cik, exchange
        FROM companies
        WHERE run_id = %s
        ORDER BY priority NULLS LAST, role, ticker
        """,
        (run_id,),
    )
    return {"run_id": run_id, "companies": rows}


@app.get("/api/runs/{run_id}/edges")
async def edges(
    run_id: str,
    request: Request,
    company: str = "",
    relation: str = "",
    modality: str = "",
    q: str = "",
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where, params = build_filters(
        run_id,
        company,
        relation,
        modality,
        q,
        subject_col="subject",
    )
    rows = await fetch_all(
        request,
        f"""
        SELECT subject, object, relation_type, modality,
               first_seen::text AS first_seen, last_seen::text AS last_seen,
               evidence_count, avg_confidence, forms, accessions, source_urls
        FROM graph_edges
        WHERE {where}
        ORDER BY evidence_count DESC NULLS LAST, subject, relation_type
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    return {"run_id": run_id, "edges": rows, "limit": limit, "offset": offset}


@app.get("/api/runs/{run_id}/evidence")
async def evidence(
    run_id: str,
    request: Request,
    company: str = "",
    relation: str = "",
    modality: str = "",
    q: str = "",
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where, params = build_filters(
        run_id,
        company,
        relation,
        modality,
        q,
        subject_col="subject",
        q_columns=(
            "subject",
            "object",
            "relation_type",
            "evidence_text",
            "source_section",
            "accession_number",
        ),
    )
    rows = await fetch_all(
        request,
        f"""
        SELECT subject, object, relation_type, direction, modality, certainty, temporal_scope,
               evidence_text, confidence_score, extractor_model_version, ticker, cik, form,
               filing_date::text AS filing_date, accepted_timestamp, accession_number,
               source_document_url, source_section, passage_id, paragraph_offset,
               parser_name, parser_version, source_document, source_document_type
        FROM relation_evidence
        WHERE {where}
        ORDER BY filing_date DESC NULLS LAST, subject, relation_type
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )
    return {"run_id": run_id, "evidence": rows, "limit": limit, "offset": offset}


@app.get("/api/runs/{run_id}/dashboard-data")
async def dashboard_data(run_id: str, request: Request) -> dict[str, Any]:
    run = await fetch_one(request, "SELECT run_id FROM runs WHERE run_id = %s", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    edge_rows = await fetch_all(
        request,
        """
        SELECT subject, object, relation_type, modality,
               first_seen::text AS first_seen, last_seen::text AS last_seen,
               evidence_count, avg_confidence, forms, accessions, source_urls
        FROM graph_edges
        WHERE run_id = %s
        ORDER BY evidence_count DESC NULLS LAST, subject, relation_type
        """,
        (run_id,),
    )
    evidence_rows = await fetch_all(
        request,
        """
        SELECT subject, object, relation_type, direction, modality, certainty, temporal_scope,
               evidence_text, confidence_score, extractor_model_version, ticker, cik, form,
               filing_date::text AS filing_date, accepted_timestamp, accession_number,
               source_document_url, source_section, passage_id, paragraph_offset,
               parser_name, parser_version, source_document, source_document_type
        FROM relation_evidence
        WHERE run_id = %s
        ORDER BY filing_date DESC NULLS LAST, subject, relation_type
        """,
        (run_id,),
    )
    company_rows = await fetch_all(
        request,
        """
        SELECT ticker, company_name, role, priority, notes, cik, exchange
        FROM companies
        WHERE run_id = %s
        ORDER BY priority NULLS LAST, role, ticker
        """,
        (run_id,),
    )
    source_document_rows = await fetch_all(
        request,
        """
        SELECT ticker, cik, company_name, form, accession_number, filing_date::text AS filing_date,
               report_date, accepted_timestamp, archive_url, document, document_type, description,
               sequence, document_url, local_path, sha256, is_primary
        FROM source_documents
        WHERE run_id = %s
        ORDER BY ticker, filing_date DESC NULLS LAST, accession_number, is_primary DESC, sequence
        """,
        (run_id,),
    )
    activity_rows = await fetch_all(
        request,
        """
        SELECT c.company_name,
               COALESCE(f.filing_count, 0)::int AS filing_count,
               COALESCE(d.source_document_count, 0)::int AS source_document_count,
               COALESCE(d.exhibit_document_count, 0)::int AS exhibit_document_count,
               COALESCE(p.passage_count, 0)::int AS passage_count,
               COALESCE(p.candidate_passage_count, 0)::int AS candidate_passage_count
        FROM companies c
        LEFT JOIN (
            SELECT company_name, COUNT(*) AS filing_count
            FROM filings
            WHERE run_id = %s
            GROUP BY company_name
        ) f ON f.company_name = c.company_name
        LEFT JOIN (
            SELECT company_name,
                   COUNT(*) AS source_document_count,
                   COUNT(*) FILTER (WHERE NOT is_primary) AS exhibit_document_count
            FROM source_documents
            WHERE run_id = %s
            GROUP BY company_name
        ) d ON d.company_name = c.company_name
        LEFT JOIN (
            SELECT company_name,
                   COUNT(*) AS passage_count,
                   COUNT(*) FILTER (WHERE is_candidate) AS candidate_passage_count
            FROM passages
            WHERE run_id = %s
            GROUP BY company_name
        ) p ON p.company_name = c.company_name
        WHERE c.run_id = %s
        """,
        (run_id, run_id, run_id, run_id),
    )
    edges = [GraphEdge(**row) for row in edge_rows]
    records = [RelationEvidence(**row) for row in evidence_rows]
    companies = [Company(**row) for row in company_rows]
    source_documents = [SourceDocument(**row) for row in source_document_rows]
    company_activity = {
        row["company_name"]: {
            "filing_count": row["filing_count"],
            "source_document_count": row["source_document_count"],
            "exhibit_document_count": row["exhibit_document_count"],
            "passage_count": row["passage_count"],
            "candidate_passage_count": row["candidate_passage_count"],
        }
        for row in activity_rows
    }
    return build_dashboard_data(
        edges,
        records,
        companies=companies,
        source_documents=source_documents,
        company_activity=company_activity,
    )


def build_filters(
    run_id: str,
    company: str,
    relation: str,
    modality: str,
    q: str,
    subject_col: str,
    q_columns: tuple[str, ...] = ("subject", "object", "relation_type"),
) -> tuple[str, tuple[Any, ...]]:
    clauses = ["run_id = %s"]
    params: list[Any] = [run_id]
    if company:
        clauses.append(f"{subject_col} = %s")
        params.append(company)
    if relation:
        clauses.append("relation_type = %s")
        params.append(relation)
    if modality:
        clauses.append("modality = %s")
        params.append(modality)
    if q:
        clauses.append("(" + " OR ".join(f"{column} ILIKE %s" for column in q_columns) + ")")
        like = f"%{q}%"
        params.extend([like] * len(q_columns))
    return " AND ".join(clauses), tuple(params)


async def fetch_all(request: Request, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with request.app.state.pool.connection() as conn:
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def fetch_one(request: Request, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    async with request.app.state.pool.connection() as conn:
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()
    return dict(row) if row else None


@app.get("/{full_path:path}", include_in_schema=False)
async def frontend_spa_fallback(full_path: str) -> FileResponse:
    reserved = {"api", "docs", "redoc", "openapi.json"}
    if full_path.split("/", 1)[0] in reserved:
        raise HTTPException(status_code=404, detail="Not found")
    if not FRONTEND_INDEX.is_file():
        raise HTTPException(status_code=404, detail="Frontend build is not available")
    return FileResponse(FRONTEND_INDEX)


def main() -> None:
    uvicorn.run("valuechain.api:app", host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    main()
