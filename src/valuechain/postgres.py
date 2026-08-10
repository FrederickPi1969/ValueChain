from __future__ import annotations

from pathlib import Path
from typing import Any

from valuechain.models import Company, EntityMention, FilingRecord, GraphEdge, MentionCluster, Passage, RelationEvidence, SourceDocument


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def load_relationship_audits_from_postgres(database_url: str, run_id: str) -> list[dict[str, Any]]:
    """Recover durable audit history when a local artifact is incomplete."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        rows = conn.execute(
            "SELECT audit FROM relationship_audits WHERE run_id = %s ORDER BY updated_at, relationship_id",
            (run_id,),
        ).fetchall()
    return [dict(row[0]) for row in rows]


def sync_relationship_lineage_to_postgres(database_url: str, run_id: str, events: list[dict[str, Any]]) -> None:
    """Append immutable lineage snapshots; duplicate event ids are harmless."""
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn.transaction():
            conn.cursor().executemany(
                "INSERT INTO relationship_lineage_events (event_id, run_id, relationship_id, stage, event) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
                [(row["event_id"], run_id, row["relationship_id"], row["stage"], Json(row)) for row in events],
            )


def sync_canonical_layer_to_postgres(
    database_url: str, run_id: str, entities: list[dict[str, Any]], relationships: list[dict[str, Any]]
) -> None:
    """Upsert the current canonical graph without deleting audit history.

    A refresh can replace relationship ids after better canonicalization. Rows not
    in this refresh become inactive rather than being deleted, so their linked
    audit records remain reproducible and API consumers only see current rows.
    """
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn.transaction():
            conn.execute("UPDATE canonical_relationships SET is_active = false WHERE run_id = %s", (run_id,))
            conn.cursor().executemany(
                """
                INSERT INTO canonical_entities
                (run_id, entity_id, canonical_name, ticker, cik, role, entity_kind, resolution_status, parent_entity_id, parent_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, entity_id) DO UPDATE SET
                  canonical_name=EXCLUDED.canonical_name, ticker=EXCLUDED.ticker, cik=EXCLUDED.cik,
                  role=EXCLUDED.role, entity_kind=EXCLUDED.entity_kind, resolution_status=EXCLUDED.resolution_status,
                  parent_entity_id=EXCLUDED.parent_entity_id, parent_name=EXCLUDED.parent_name
                """,
                [(run_id, row.get("entity_id"), row.get("canonical_name"), row.get("ticker"), row.get("cik"), row.get("role"), row.get("entity_kind"), row.get("resolution_status"), row.get("parent_entity_id"), row.get("parent_name")) for row in entities],
            )
            conn.cursor().executemany(
                """
                INSERT INTO canonical_relationships
                (run_id, relationship_id, supplier_entity_id, supplier_name, customer_entity_id, customer_name,
                 source_entity_id, source_entity_name, source_role, target_entity_id, target_entity_name, target_role,
                 relationship_type, relationship_family, product_or_service, categories, source_relation_types,
                 modality, confidence, evidence_count, evidence_ids, issuer_names, source_accession_numbers,
                 source_types, first_observed_date, last_observed_date, verification_status, review_status,
                 decision, decision_source, decision_reason, human_review, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NULLIF(%s, '')::date, NULLIF(%s, '')::date, %s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (run_id, relationship_id) DO UPDATE SET
                  supplier_entity_id=EXCLUDED.supplier_entity_id, supplier_name=EXCLUDED.supplier_name,
                  customer_entity_id=EXCLUDED.customer_entity_id, customer_name=EXCLUDED.customer_name,
                  source_entity_id=EXCLUDED.source_entity_id, source_entity_name=EXCLUDED.source_entity_name, source_role=EXCLUDED.source_role,
                  target_entity_id=EXCLUDED.target_entity_id, target_entity_name=EXCLUDED.target_entity_name, target_role=EXCLUDED.target_role,
                  relationship_type=EXCLUDED.relationship_type, relationship_family=EXCLUDED.relationship_family,
                  product_or_service=EXCLUDED.product_or_service, categories=EXCLUDED.categories, source_relation_types=EXCLUDED.source_relation_types,
                  modality=EXCLUDED.modality, confidence=EXCLUDED.confidence, evidence_count=EXCLUDED.evidence_count,
                  evidence_ids=EXCLUDED.evidence_ids, issuer_names=EXCLUDED.issuer_names, source_accession_numbers=EXCLUDED.source_accession_numbers,
                  source_types=EXCLUDED.source_types, first_observed_date=EXCLUDED.first_observed_date, last_observed_date=EXCLUDED.last_observed_date,
                  verification_status=EXCLUDED.verification_status, review_status=EXCLUDED.review_status, decision=EXCLUDED.decision,
                  decision_source=EXCLUDED.decision_source, decision_reason=EXCLUDED.decision_reason, human_review=EXCLUDED.human_review, is_active=true
                """,
                [
                    (run_id, row.get("relationship_id"), row.get("supplier_entity_id"), row.get("supplier_name"), row.get("customer_entity_id"), row.get("customer_name"),
                     row.get("source_entity_id"), row.get("source_entity_name"), row.get("source_role"), row.get("target_entity_id"), row.get("target_entity_name"), row.get("target_role"),
                     row.get("relationship_type"), row.get("relationship_family"), row.get("product_or_service"), Json(row.get("categories", [])), Json(row.get("source_relation_types", [])),
                     row.get("modality"), row.get("confidence"), row.get("evidence_count"), Json(row.get("evidence_ids", [])), Json(row.get("issuer_names", [])),
                     Json(row.get("source_accession_numbers", [])), Json(row.get("source_types", [])), row.get("first_observed_date"), row.get("last_observed_date"),
                     row.get("verification_status"), row.get("review_status", "unreviewed"), row.get("decision"), row.get("decision_source"), row.get("decision_reason"),
                     Json(row["human_review"]) if row.get("human_review") else None)
                    for row in relationships
                ],
            )
            conn.cursor().executemany(
                "UPDATE canonical_relationships SET risk_flags = %s WHERE run_id = %s AND relationship_id = %s",
                [(Json(row.get("risk_flags", [])), run_id, row.get("relationship_id")) for row in relationships],
            )


def append_entity_resolution_decision_events_to_postgres(
    database_url: str, run_id: str, events: list[dict[str, Any]]
) -> None:
    """Append decision-engine outcomes; resolution records remain a mutable queue."""
    if not events:
        return
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn.transaction():
            conn.cursor().executemany(
                """INSERT INTO entity_resolution_decision_events
                (run_id, resolution_id, decision, decision_source, decision_reason, event)
                VALUES (%s, %s, %s, %s, %s, %s)""",
                [(run_id, row.get("resolution_id", row.get("query_object", "")), row.get("decision", "PENDING"), "decision_engine", row.get("policy_reason", ""), Json(row)) for row in events],
            )


def sync_relationship_audits_to_postgres(database_url: str, run_id: str, audits: list[dict[str, Any]]) -> None:
    """Upsert later LLM audits without replacing the underlying shared run."""
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn.transaction():
            conn.cursor().executemany(
                """
                INSERT INTO relationship_audits (run_id, relationship_id, audit)
                VALUES (%s, %s, %s)
                ON CONFLICT (run_id, relationship_id) DO UPDATE
                SET audit = EXCLUDED.audit, updated_at = now()
                """,
                [(run_id, row.get("relationship_id"), Json(row)) for row in audits],
            )


def sync_relationship_reviews_to_postgres(
    database_url: str, run_id: str, relationships: list[dict[str, Any]]
) -> None:
    """Make CSV-based team review decisions visible to every API consumer."""
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn.transaction():
            conn.cursor().executemany(
                """
                UPDATE canonical_relationships
                SET review_status = %s, human_review = %s,
                    decision = CASE WHEN %s = 'accepted' THEN 'accept' WHEN %s = 'rejected' THEN 'reject' ELSE decision END,
                    decision_source = CASE WHEN %s IN ('accepted', 'rejected', 'needs_review') THEN 'human_review' ELSE decision_source END
                WHERE run_id = %s AND relationship_id = %s
                """,
                [
                    (
                        row.get("review_status", "unreviewed"), Json(row.get("human_review")) if row.get("human_review") else None,
                        row.get("review_status", "unreviewed"), row.get("review_status", "unreviewed"),
                        row.get("review_status", "unreviewed"), run_id, row.get("relationship_id"),
                    )
                    for row in relationships
                ],
            )


def sync_mention_layer_to_postgres(
    database_url: str, run_id: str, mentions: list[EntityMention], clusters: list[MentionCluster]
) -> None:
    """Refresh only the append-only mention and alias-cluster layer for a run."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn.transaction():
            conn.execute("DELETE FROM entity_mentions WHERE run_id = %s", (run_id,))
            conn.execute("DELETE FROM mention_clusters WHERE run_id = %s", (run_id,))
            _insert_mention_layer(conn, run_id, mentions, clusters)


def sync_entity_resolution_records_to_postgres(database_url: str, run_id: str, records: list[dict[str, Any]]) -> None:
    """Publish current resolution work queue without erasing its decision-event audit log."""
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn.transaction():
            conn.execute("DELETE FROM entity_resolution_records WHERE run_id = %s", (run_id,))
            conn.cursor().executemany(
                """INSERT INTO entity_resolution_records
                (run_id, resolution_id, record, resolution_status, decision, priority_score)
                VALUES (%s, %s, %s, %s, %s, %s)""",
                [(run_id, row["resolution_id"], Json(row), row.get("resolution_status", "unresolved"), row.get("decision", "PENDING"), row.get("priority_score", 0)) for row in records],
            )


def write_run_to_postgres(
    database_url: str,
    run_id: str,
    summary: dict[str, Any],
    companies: list[Company],
    filings: list[FilingRecord],
    source_documents: list[SourceDocument],
    passages: list[Passage],
    candidate_passages: list[Passage],
    evidence: list[RelationEvidence],
    edges: list[GraphEdge],
    canonical_entities: list[dict[str, Any]] | None = None,
    canonical_relationships: list[dict[str, Any]] | None = None,
    relationship_audits: list[dict[str, Any]] | None = None,
    entity_mentions: list[EntityMention] | None = None,
    mention_clusters: list[MentionCluster] | None = None,
) -> None:
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError as exc:
        raise RuntimeError("Postgres export requires psycopg. Run `pip install -e .`.") from exc

    candidate_ids = {passage.passage_id for passage in candidate_passages}
    source_documents = source_documents or []
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
            for table in ["relationship_audits", "canonical_relationships", "canonical_entities", "graph_edges", "relation_evidence", "entity_mentions", "mention_clusters", "passages", "source_documents", "filings", "companies"]:
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
            _insert_mention_layer(conn, run_id, entity_mentions or [], mention_clusters or [])
            conn.cursor().executemany(
                """
                INSERT INTO canonical_entities
                (run_id, entity_id, canonical_name, ticker, cik, role, entity_kind,
                 resolution_status, parent_entity_id, parent_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (run_id, row.get("entity_id"), row.get("canonical_name"), row.get("ticker"), row.get("cik"),
                     row.get("role"), row.get("entity_kind"), row.get("resolution_status"),
                     row.get("parent_entity_id"), row.get("parent_name"))
                    for row in canonical_entities or []
                ],
            )
            conn.cursor().executemany(
                """
                INSERT INTO canonical_relationships
                (run_id, relationship_id, supplier_entity_id, supplier_name, customer_entity_id, customer_name,
                 source_entity_id, source_entity_name, source_role, target_entity_id, target_entity_name, target_role,
                 relationship_type, relationship_family, product_or_service, categories, source_relation_types,
                 modality, confidence, evidence_count, evidence_ids, issuer_names, source_accession_numbers,
                 source_types, first_observed_date, last_observed_date, verification_status, review_status,
                 decision, decision_source, decision_reason, human_review)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NULLIF(%s, '')::date, NULLIF(%s, '')::date, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id, row.get("relationship_id"), row.get("supplier_entity_id"), row.get("supplier_name"),
                        row.get("customer_entity_id"), row.get("customer_name"), row.get("source_entity_id"),
                        row.get("source_entity_name"), row.get("source_role"), row.get("target_entity_id"),
                        row.get("target_entity_name"), row.get("target_role"), row.get("relationship_type"),
                        row.get("relationship_family"), row.get("product_or_service"), Json(row.get("categories", [])),
                        Json(row.get("source_relation_types", [])), row.get("modality"), row.get("confidence"),
                        row.get("evidence_count"), Json(row.get("evidence_ids", [])), Json(row.get("issuer_names", [])),
                        Json(row.get("source_accession_numbers", [])), Json(row.get("source_types", [])),
                        row.get("first_observed_date"), row.get("last_observed_date"), row.get("verification_status"),
                        row.get("review_status", "unreviewed"), row.get("decision"), row.get("decision_source"),
                        row.get("decision_reason"), Json(row["human_review"]) if row.get("human_review") else None,
                    )
                    for row in canonical_relationships or []
                ],
            )
            conn.cursor().executemany(
                """
                INSERT INTO relationship_audits (run_id, relationship_id, audit)
                VALUES (%s, %s, %s)
                """,
                [(run_id, row.get("relationship_id"), Json(row)) for row in relationship_audits or []],
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
                INSERT INTO source_documents
                (run_id, document_id, accession_number, ticker, cik, company_name, form, filing_date,
                 report_date, accepted_timestamp, archive_url, document, document_type, description,
                 sequence, document_url, local_path, sha256, is_primary)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NULLIF(%s, '')::date,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id,
                        document.document_id(),
                        document.accession_number,
                        document.ticker,
                        document.cik,
                        document.company_name,
                        document.form,
                        document.filing_date,
                        document.report_date,
                        document.accepted_timestamp,
                        document.archive_url,
                        document.document,
                        document.document_type,
                        document.description,
                        document.sequence,
                        document.document_url,
                        document.local_path,
                        document.sha256,
                        document.is_primary,
                    )
                    for document in source_documents
                ],
            )
            conn.cursor().executemany(
                """
                INSERT INTO passages
                (run_id, passage_id, accession_number, ticker, cik, company_name, form, filing_date,
                 source_document_url, source_document, source_document_type, section, paragraph_offset,
                 text, parser_name, parser_version, relevance_score, relevance_terms, is_candidate)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NULLIF(%s, '')::date, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
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
                        passage.source_document_url,
                        passage.source_document,
                        passage.source_document_type,
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
                 paragraph_offset, parser_name, parser_version, source_document, source_document_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULLIF(%s, '')::date,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        row.source_document,
                        row.source_document_type,
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


def _insert_mention_layer(conn: Any, run_id: str, mentions: list[EntityMention], clusters: list[MentionCluster]) -> None:
    """Insert deterministic mention provenance and pre-canonical alias clusters."""
    conn.cursor().executemany(
        """
        INSERT INTO mention_clusters
        (run_id, cluster_id, normalized_key, representative_name, proposed_canonical_name,
         canonical_entity_id, entity_type, resolution_status, resolver_method, mention_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (run_id, row.cluster_id, row.normalized_key, row.representative_name, row.proposed_canonical_name,
             row.canonical_entity_id, row.entity_type, row.resolution_status, row.resolver_method, row.mention_count)
            for row in clusters
        ],
    )
    conn.cursor().executemany(
        """
        INSERT INTO entity_mentions
        (run_id, mention_id, passage_id, text, normalized_name, entity_type, ticker, cik,
         confidence, start_offset, end_offset, mention_kind, resolution_status, resolver_method,
         cluster_id, canonical_entity_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (run_id, row.mention_id, row.passage_id, row.text, row.normalized_name, row.entity_type,
             row.ticker, row.cik, row.confidence, row.start_offset, row.end_offset, row.mention_kind,
             row.resolution_status, row.resolver_method, row.cluster_id, row.canonical_entity_id)
            for row in mentions
        ],
    )
