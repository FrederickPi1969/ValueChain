from __future__ import annotations

from pathlib import Path
from typing import Any

from valuechain.models import Company, FilingRecord, GraphEdge, Passage, RelationEvidence


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def write_run_to_postgres(
    database_url: str,
    run_id: str,
    summary: dict[str, Any],
    companies: list[Company],
    filings: list[FilingRecord],
    passages: list[Passage],
    candidate_passages: list[Passage],
    evidence: list[RelationEvidence],
    edges: list[GraphEdge],
) -> None:
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc

    candidate_ids = {passage.passage_id for passage in candidate_passages}
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO runs (run_id, run_label, options, counts, summary)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                SET run_label = EXCLUDED.run_label,
                    options = EXCLUDED.options,
                    counts = EXCLUDED.counts,
                    summary = EXCLUDED.summary
                """,
                (
                    run_id,
                    summary.get("run_label", run_id),
                    Json(summary.get("options", {})),
                    Json(summary.get("counts", {})),
                    Json(summary),
                ),
            )
            for table in ["graph_edges", "relation_evidence", "passages", "filings", "companies"]:
                conn.execute(f"DELETE FROM {table} WHERE run_id = %s", (run_id,))

            conn.cursor().executemany(
                """
                INSERT INTO companies
                (run_id, ticker, company_name, role, priority, notes, cik, exchange)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id,
                        company.ticker,
                        company.company_name,
                        company.role,
                        company.priority,
                        company.notes,
                        company.cik,
                        company.exchange,
                    )
                    for company in companies
                ],
            )
            conn.cursor().executemany(
                """
                INSERT INTO filings
                (run_id, accession_number, ticker, cik, company_name, form, filing_date, report_date,
                 accepted_timestamp, primary_document, archive_url, primary_document_url, local_path, sha256)
                VALUES (%s, %s, %s, %s, %s, %s, NULLIF(%s, '')::date, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id,
                        filing.accession_number,
                        filing.ticker,
                        filing.cik,
                        filing.company_name,
                        filing.form,
                        filing.filing_date,
                        filing.report_date,
                        filing.accepted_timestamp,
                        filing.primary_document,
                        filing.archive_url,
                        filing.primary_document_url,
                        filing.local_path,
                        filing.sha256,
                    )
                    for filing in filings
                ],
            )
            conn.cursor().executemany(
                """
                INSERT INTO passages
                (run_id, passage_id, accession_number, ticker, cik, company_name, form, filing_date,
                 section, paragraph_offset, text, parser_name, parser_version, relevance_score,
                 relevance_terms, is_candidate)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NULLIF(%s, '')::date, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id,
                        passage.passage_id,
                        passage.accession_number,
                        passage.ticker,
                        passage.cik,
                        passage.company_name,
                        passage.form,
                        passage.filing_date,
                        passage.section,
                        passage.paragraph_offset,
                        passage.text,
                        passage.parser_name,
                        passage.parser_version,
                        passage.relevance_score,
                        passage.relevance_terms,
                        passage.passage_id in candidate_ids,
                    )
                    for passage in passages
                ],
            )
            conn.cursor().executemany(
                """
                INSERT INTO relation_evidence
                (run_id, subject, object, relation_type, direction, modality, certainty, temporal_scope,
                 evidence_text, confidence_score, extractor_model_version, ticker, cik, form, filing_date,
                 accepted_timestamp, accession_number, source_document_url, source_section, passage_id,
                 paragraph_offset, parser_name, parser_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULLIF(%s, '')::date,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id,
                        row.subject,
                        row.object,
                        row.relation_type,
                        row.direction,
                        row.modality,
                        row.certainty,
                        row.temporal_scope,
                        row.evidence_text,
                        row.confidence_score,
                        row.extractor_model_version,
                        row.ticker,
                        row.cik,
                        row.form,
                        row.filing_date,
                        row.accepted_timestamp,
                        row.accession_number,
                        row.source_document_url,
                        row.source_section,
                        row.passage_id,
                        row.paragraph_offset,
                        row.parser_name,
                        row.parser_version,
                    )
                    for row in evidence
                ],
            )
            conn.cursor().executemany(
                """
                INSERT INTO graph_edges
                (run_id, subject, object, relation_type, modality, first_seen, last_seen, evidence_count,
                 avg_confidence, forms, accessions, source_urls)
                VALUES (%s, %s, %s, %s, %s, NULLIF(%s, '')::date, NULLIF(%s, '')::date, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id,
                        edge.subject,
                        edge.object,
                        edge.relation_type,
                        edge.modality,
                        edge.first_seen,
                        edge.last_seen,
                        edge.evidence_count,
                        edge.avg_confidence,
                        edge.forms,
                        edge.accessions,
                        edge.source_urls,
                    )
                    for edge in edges
                ],
            )
