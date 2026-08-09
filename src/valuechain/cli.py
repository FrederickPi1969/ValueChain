from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from valuechain.canonicalization import build_canonical_layer, relationship_review_queue
from valuechain.config import MAX_LLM_CONCURRENCY, Settings, ensure_dirs
from valuechain.dashboard import canonical_network_edges
from valuechain.evaluation import evaluate_canonical_relationships, load_gold
from valuechain.evidence_audit import apply_latest_audit_decisions, audit_canonical_relationships, audit_summary, attach_audits_to_dashboard, attach_enrichment_to_dashboard, build_direction_correction_proposals, merge_audit_history, migrate_relationship_evidence_ids
from valuechain.human_review import VALID_STATUSES, apply_human_reviews, inherit_prior_reviews, publish_human_review_to_dashboard, read_review_csv
from valuechain.gleif import (
    EntityObjectContext,
    GLEIFClient,
    resolve_object_contexts,
    run_gleif_resolution,
    select_best_matches_with_llm,
    write_candidate_queue,
    write_llm_selection_queue,
)
from valuechain.alias_decision_policy import decide_alias_resolutions
from valuechain.io_utils import read_jsonl, write_csv, write_json, write_jsonl
from valuechain.llm_client import LLMConfig, OpenAICompatibleClient
from valuechain.lineage import merge_lineage_history, relationship_lineage_events
from valuechain.relationship_projection import publish_relationship_projection
from valuechain.models import RelationEvidence
from valuechain.models import EntityMention, MentionCluster, Passage
from valuechain.mention_layer import build_alias_review_queue, build_entity_triage, build_mention_clusters, extract_passage_mentions
from valuechain.mention_constrained_extraction import constrain_relation_records
from valuechain.resolution_records import attach_internal_resolution_candidates, build_resolution_records
from valuechain.reextraction import create_reextraction_preview
from valuechain.postgres import append_entity_resolution_decision_events_to_postgres, load_relationship_audits_from_postgres, sync_canonical_layer_to_postgres, sync_entity_resolution_records_to_postgres, sync_mention_layer_to_postgres, sync_relationship_audits_to_postgres, sync_relationship_lineage_to_postgres, sync_relationship_reviews_to_postgres
from valuechain.pipeline import PipelineOptions, run_pipeline
from valuechain.planning import build_execution_plan
from valuechain.run_registry import read_run_registry, sync_frontend_public_data
from valuechain.universe import parse_csv_arg, parse_tickers, read_universe, summarize_universe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valuechain",
        description="Prototype SEC filing to dependency evidence pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    universe = sub.add_parser("universe", help="Inspect the configured company universe before running.")
    add_input_args(universe)
    universe.add_argument("--json", action="store_true", help="Print full universe metadata as JSON.")

    plan = sub.add_parser("plan", help="Build an execution plan without SEC network downloads.")
    add_input_args(plan)
    add_run_shape_args(plan)
    plan.add_argument("--write", action="store_true", help="Write data/processed/input_plan.json.")

    run = sub.add_parser("run", help="Run the end-to-end prototype pipeline.")
    add_input_args(run)
    add_run_shape_args(run)
    run.add_argument("--extractor", choices=["rules", "llm", "hybrid"], default="rules")
    run.add_argument("--min-relevance-score", type=float, default=2.0)
    run.add_argument("--skip-yahoo", action="store_true")
    run.add_argument("--run-id", default="", help="Stable id for this run. Defaults to a timestamped id.")
    run.add_argument("--run-label", default="", help="Human-readable label shown in the frontend run index.")
    run.add_argument("--write-postgres", action="store_true", help="Write run artifacts into Postgres.")
    run.add_argument("--postgres-url", default="", help="Override VALUECHAIN_DATABASE_URL for this run.")
    run.add_argument("--llm-concurrency", type=int, default=None, help="Concurrent LLM extraction requests.")
    run.add_argument("--embedding-merge", action="store_true", help="Use local embedding model for object alias merge.")
    run.add_argument("--embedding-threshold", type=float, default=0.92, help="Cosine threshold for embedding object merge.")
    run.add_argument(
        "--no-exhibits",
        action="store_true",
        help="Disable archive exhibit retrieval and parse only primary filing documents.",
    )
    run.add_argument(
        "--exhibit-types",
        default="EX-10,EX-21,EX-99,EX-99.1",
        help="Comma-separated exhibit type prefixes to include from SEC archive detail pages.",
    )
    run.add_argument(
        "--max-exhibits-per-filing",
        type=int,
        default=8,
        help="Maximum selected exhibit source documents per filing.",
    )

    resolve = sub.add_parser(
        "resolve-entities",
        help="Create a GLEIF-backed resolver candidate queue without modifying graph edges.",
    )
    resolve.add_argument("--run-id", default="", help="Run id whose relation_evidence.jsonl objects should be resolved.")
    resolve.add_argument("--input", default="", help="Explicit relation_evidence JSONL path. Overrides --run-id.")
    resolve.add_argument("--objects", default="", help="Comma-separated object strings to resolve without reading a run.")
    resolve.add_argument("--output-dir", default="", help="Output directory. Defaults to the selected run directory.")
    resolve.add_argument("--output-prefix", default="entity_resolution_candidates")
    resolve.add_argument("--limit-objects", type=int, default=100, help="Max unique objects to send to GLEIF.")
    resolve.add_argument("--min-evidence-count", type=int, default=2)
    resolve.add_argument("--max-candidates", type=int, default=5)
    resolve.add_argument("--gleif-rps", type=float, default=None, help="GLEIF API requests per second.")
    resolve.add_argument("--include-class-objects", action="store_true", help="Also send generic class objects to GLEIF.")
    resolve.add_argument("--include-relationships", action="store_true", help="Fetch available parent relationship records.")
    resolve.add_argument("--llm-select", action="store_true", help="Use Local LLM to choose best GLEIF candidate per object.")
    resolve.add_argument("--llm-model", default="", help="Override VALUECHAIN_EXTRACTION_MODEL for LLM candidate selection.")
    resolve.add_argument("--llm-concurrency", type=int, default=None, help="Concurrent LLM best-match selection requests.")

    audit = sub.add_parser("audit-relationships", help="Use the Local LLM to verify canonical links from their SEC evidence.")
    audit.add_argument("--run-id", required=True, help="Existing run id to audit.")
    audit.add_argument("--llm-model", default="", help="Override VALUECHAIN_COMPLEX_MODEL.")
    audit.add_argument("--llm-concurrency", type=int, default=None, help="Concurrent audit calls (maximum 16).")
    audit.add_argument("--limit", type=int, default=0, help="Audit only the first N canonical relationships.")
    audit.add_argument("--pending-only", action="store_true", help="Audit only unreviewed/needs-review relationships, leaving confirmed and rejected history untouched.")
    publish_audit = sub.add_parser("publish-relationship-audit", help="Attach a saved relationship audit to the dashboard.")
    publish_audit.add_argument("--run-id", required=True, help="Existing run id with canonical_relationship_audit.json.")
    review = sub.add_parser("apply-review", help="Import human canonical-relationship decisions into a saved run.")
    review.add_argument("--run-id", required=True, help="Existing run id to update.")
    review.add_argument("--review-csv", required=True, help="CSV exported from Resolution review.")
    enrich = sub.add_parser("enrich-relationships", help="Attach saved LLM product/service metadata to a run.")
    enrich.add_argument("--run-id", required=True, help="Existing run id with canonical_relationship_audit.json.")
    refresh_canonical = sub.add_parser("refresh-canonical", help="Rebuild canonical entities/relationships from saved evidence without re-downloading SEC filings.")
    refresh_canonical.add_argument("--run-id", required=True, help="Existing run id to refresh.")
    refresh_canonical.add_argument("--write-postgres", action="store_true", help="Also synchronize the rebuilt canonical layer to shared Postgres.")
    refresh_mentions = sub.add_parser("refresh-mentions", help="Build the persisted Mention / Cluster layer from saved passages without downloading SEC filings.")
    refresh_mentions.add_argument("--run-id", required=True, help="Existing run id to refresh.")
    refresh_mentions.add_argument("--write-postgres", action="store_true", help="Also synchronize the mention layer to shared Postgres.")
    reextract = sub.add_parser(
        "reextract-relationships",
        help="Create an isolated relationship re-extraction preview from saved SEC passages; it never overwrites audited artifacts.",
    )
    reextract.add_argument("--run-id", required=True, help="Existing run whose saved passages will be re-extracted.")
    reextract.add_argument("--extractor", choices=["rules", "llm", "hybrid"], default="rules")
    reextract.add_argument("--llm-concurrency", type=int, default=None, help="Concurrent LLM extraction requests (maximum 16).")
    reextract.add_argument("--preview-id", default="", help="Optional stable preview folder name under the run's reextractions directory.")
    preview_constraints = sub.add_parser("preview-mention-constraints", help="Measure how persisted mentions would ground a saved run's relation evidence without modifying it.")
    preview_constraints.add_argument("--run-id", required=True, help="Existing run id to inspect.")
    resolve_records = sub.add_parser("resolve-entity-records", help="Generate GLEIF candidates, assess them with the local LLM, validate and decide relation-linked entity records.")
    resolve_records.add_argument("--run-id", required=True)
    resolve_records.add_argument("--limit", type=int, default=25, help="Maximum priority-ranked legal-entity candidates to resolve.")
    resolve_records.add_argument("--llm-model", default="")
    resolve_records.add_argument("--llm-concurrency", type=int, default=None)
    inherit = sub.add_parser("inherit-reviews", help="Carry reviewed canonical decisions from prior runs into a newer run.")
    inherit.add_argument("--run-id", required=True, help="Target run id.")
    inherit.add_argument("--from-run", action="append", required=True, help="Source run id; repeat for more than one.")
    evaluate = sub.add_parser("evaluate", help="Compare a saved canonical run with a manually curated gold relationship set.")
    evaluate.add_argument("--run-id", required=True, help="Run id containing canonical_relationships.jsonl.")
    evaluate.add_argument("--gold", required=True, help="Path to a gold JSON file.")
    evaluate.add_argument("--output", default="", help="Optional JSON report path.")
    return parser


def add_input_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--universe", default="data/universe/ai_infra_universe.csv")
    parser.add_argument("--tickers", help="Comma-separated ticker subset, e.g. NVDA,AMD,MSFT.")
    parser.add_argument("--roles", help="Comma-separated role subset, e.g. foundry,cloud_hyperscaler.")
    parser.add_argument("--priority", type=int, help="Include companies with priority <= this value.")
    parser.add_argument("--limit-companies", type=int, help="Cap company count after filtering.")


def add_run_shape_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--forms", default="10-K,10-Q,8-K,20-F,6-K")
    parser.add_argument("--max-filings-per-company", type=int, default=2)
    parser.add_argument(
        "--filing-selection",
        choices=["form-balanced", "latest"],
        default="form-balanced",
        help="form-balanced takes up to max filings per selected form; latest preserves the old total latest-filings cap.",
    )
    parser.add_argument("--filing-date-from", default="", help="Inclusive YYYY-MM-DD filing date lower bound.")
    parser.add_argument("--filing-date-to", default="", help="Inclusive YYYY-MM-DD filing date upper bound.")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "universe":
        companies = read_filtered_universe(args)
        summary = summarize_universe(companies)
        if args.json:
            print(json.dumps({"summary": summary, "companies": [c.to_dict() for c in companies]}, indent=2))
            return
        print(f"companies={summary['company_count']}")
        print(f"roles={json.dumps(summary['role_counts'], sort_keys=True)}")
        print(f"priorities={json.dumps(summary['priority_counts'], sort_keys=True)}")
        print("tickers=" + ",".join(summary["tickers"]))
        return
    if args.command == "plan":
        companies = read_filtered_universe(args)
        plan = build_execution_plan(
            companies=companies,
            forms=parse_forms(args.forms),
            max_filings_per_company=args.max_filings_per_company,
            filing_selection=args.filing_selection.replace("-", "_"),
            filing_date_from=args.filing_date_from,
            filing_date_to=args.filing_date_to,
        )
        payload = plan.to_dict()
        if args.write:
            settings = Settings()
            ensure_dirs(settings)
            write_json(settings.processed_dir / "input_plan.json", payload)
        print(json.dumps(payload, indent=2))
        return
    if args.command == "run":
        settings = Settings()
        llm_concurrency = args.llm_concurrency or settings.llm_concurrency
        if not 1 <= llm_concurrency <= MAX_LLM_CONCURRENCY:
            parser.error(f"--llm-concurrency must be between 1 and {MAX_LLM_CONCURRENCY}")
        options = PipelineOptions(
            universe_path=Path(args.universe),
            tickers=parse_tickers(args.tickers),
            roles=parse_csv_arg(args.roles),
            max_priority=args.priority,
            limit_companies=args.limit_companies,
            forms=parse_forms(args.forms),
            max_filings_per_company=args.max_filings_per_company,
            filing_selection=args.filing_selection.replace("-", "_"),
            filing_date_from=args.filing_date_from,
            filing_date_to=args.filing_date_to,
            extractor=args.extractor,
            min_relevance_score=args.min_relevance_score,
            skip_yahoo=args.skip_yahoo,
            run_id=args.run_id,
            run_label=args.run_label,
            write_postgres=args.write_postgres,
            postgres_url=args.postgres_url,
            llm_concurrency=llm_concurrency,
            embedding_merge=args.embedding_merge,
            embedding_threshold=args.embedding_threshold,
            include_exhibits=not args.no_exhibits,
            exhibit_types=parse_forms(args.exhibit_types),
            max_exhibits_per_filing=args.max_exhibits_per_filing,
        )
        result = run_pipeline(settings, options)
        print(f"run_id={result.run_id}")
        print(f"companies={len(result.companies)}")
        print(f"filings={len(result.filings)}")
        print(f"source_documents={len(result.source_documents)}")
        print(f"exhibit_documents={sum(1 for document in result.source_documents if not document.is_primary)}")
        print(f"passages={len(result.passages)}")
        print(f"candidate_passages={len(result.candidate_passages)}")
        print(f"relation_evidence={len(result.evidence)}")
        print(f"graph_edges={len(result.edges)}")
        print(f"dashboard={result.dashboard_path}")
        print(f"frontend_index={result.index_path}")
        return
    if args.command == "resolve-entities":
        settings = Settings()
        ensure_dirs(settings)
        objects = parse_csv_arg(args.objects)
        evidence_path = Path(args.input) if args.input else None
        if not evidence_path and args.run_id:
            evidence_path = settings.processed_dir / "runs" / args.run_id / "relation_evidence.jsonl"
        if not objects and (not evidence_path or not evidence_path.exists()):
            parser.error("--run-id, --input, or --objects is required; relation_evidence.jsonl must exist for run/input mode.")
        output_dir = Path(args.output_dir) if args.output_dir else (
            evidence_path.parent if evidence_path else settings.processed_dir / "entity_resolution"
        )
        client = GLEIFClient(
            requests_per_second=args.gleif_rps if args.gleif_rps is not None else settings.gleif_rps,
            proxies=settings.proxies,
        )
        result = run_gleif_resolution(
            evidence_path=evidence_path,
            objects=objects,
            output_dir=output_dir,
            client=client,
            limit_objects=args.limit_objects,
            min_evidence_count=args.min_evidence_count,
            max_candidates=args.max_candidates,
            include_class_objects=args.include_class_objects,
            include_relationships=args.include_relationships,
            output_prefix=args.output_prefix,
        )
        print(f"objects={len(result['contexts'])}")
        print(f"candidate_rows={len(result['candidates'])}")
        for name, path in result["paths"].items():
            print(f"{name}={path}")
        if args.llm_select:
            llm_concurrency = args.llm_concurrency or settings.llm_concurrency
            if not 1 <= llm_concurrency <= MAX_LLM_CONCURRENCY:
                parser.error(f"--llm-concurrency must be between 1 and {MAX_LLM_CONCURRENCY}")
            llm_model = args.llm_model or settings.extraction_model
            llm_client = OpenAICompatibleClient(
                LLMConfig(
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=llm_model,
                    report_url="",
                    proxy_url=settings.https_proxy or settings.http_proxy,
                    max_connections=llm_concurrency,
                    max_keepalive_connections=min(8, llm_concurrency),
                )
            )
            selections = select_best_matches_with_llm(
                result["candidates"],
                llm_client=llm_client,
                model_version=llm_model,
                concurrency=llm_concurrency,
            )
            selection_paths = write_llm_selection_queue(output_dir, selections)
            print(f"llm_selection_rows={len(selections)}")
            for name, path in selection_paths.items():
                print(f"llm_{name}={path}")
        return
    if args.command == "audit-relationships":
        settings = Settings()
        ensure_dirs(settings)
        concurrency = args.llm_concurrency or settings.llm_concurrency
        if not 1 <= concurrency <= MAX_LLM_CONCURRENCY:
            parser.error(f"--llm-concurrency must be between 1 and {MAX_LLM_CONCURRENCY}")
        run_dir = settings.processed_dir / "runs" / args.run_id
        relationships = read_jsonl(run_dir / "canonical_relationships.jsonl")
        all_relationships = list(relationships)
        evidence = read_jsonl(run_dir / "relation_evidence.jsonl")
        if not relationships or not evidence:
            parser.error("This run needs canonical_relationships.jsonl and relation_evidence.jsonl.")
        if args.pending_only:
            relationships = [row for row in relationships if row.get("review_status") in {"unreviewed", "needs_review"}]
        if args.limit > 0:
            relationships = relationships[:args.limit]
        prior_audit_path = run_dir / "canonical_relationship_audit.json"
        local_audits = json.loads(prior_audit_path.read_text(encoding="utf-8")).get("rows", []) if prior_audit_path.exists() else []
        # Postgres is the shared, durable source: retain rows that an older local
        # JSON artifact accidentally omitted.
        prior_by_id = {str(row.get("relationship_id", "")): row for row in load_relationship_audits_from_postgres(settings.database_url, args.run_id)}
        prior_by_id.update({str(row.get("relationship_id", "")): row for row in local_audits})
        prior_audits = list(prior_by_id.values())
        model = args.llm_model or settings.complex_model
        client = OpenAICompatibleClient(LLMConfig(
            base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=model,
            report_url="", proxy_url=settings.https_proxy or settings.http_proxy,
            max_connections=concurrency, max_keepalive_connections=min(8, concurrency),
        ))
        new_audits = merge_audit_history(audit_canonical_relationships(relationships, evidence, client, model, concurrency), prior_audits)
        new_by_id = {str(row.get("relationship_id", "")): row for row in new_audits}
        audits = [new_by_id.pop(str(row.get("relationship_id", "")), row) for row in prior_audits]
        audits.extend(new_by_id.values())
        write_json(run_dir / "canonical_relationship_audit.json", {"summary": audit_summary(audits), "rows": audits})
        write_csv(run_dir / "canonical_relationship_audit.csv", audits)
        updated_relationships = apply_latest_audit_decisions(all_relationships, audits)
        proposals = build_direction_correction_proposals(all_relationships, audits)
        existing_ids = {str(row.get("relationship_id", "")) for row in updated_relationships}
        updated_relationships.extend(row for row in proposals if str(row.get("relationship_id", "")) not in existing_ids)
        write_jsonl(run_dir / "direction_correction_proposals.jsonl", proposals)
        write_jsonl(run_dir / "canonical_relationships.jsonl", updated_relationships)
        write_jsonl(run_dir / "canonical_relationships_reviewed.jsonl", updated_relationships)
        lineage = merge_lineage_history(read_jsonl(run_dir / "relationship_lineage_events.jsonl"), relationship_lineage_events(updated_relationships, "audit_applied"))
        write_jsonl(run_dir / "relationship_lineage_events.jsonl", lineage)
        write_csv(run_dir / "relationship_review_queue.csv", relationship_review_queue(updated_relationships))
        entities = read_jsonl(run_dir / "canonical_entities.jsonl")
        publish_relationship_projection(settings, args.run_id, entities, updated_relationships, lineage, audits)
        published = publish_relationship_audit(settings, args.run_id, audits)
        print(json.dumps(audit_summary(audits), ensure_ascii=False))
        print(f"audit={run_dir / 'canonical_relationship_audit.csv'}")
        print(f"dashboard_updated={published}")
        return
    if args.command == "publish-relationship-audit":
        settings = Settings()
        audit_path = settings.processed_dir / "runs" / args.run_id / "canonical_relationship_audit.json"
        if not audit_path.exists():
            parser.error("This run needs canonical_relationship_audit.json. Run audit-relationships first.")
        audits = json.loads(audit_path.read_text(encoding="utf-8")).get("rows", [])
        print(f"dashboard_updated={publish_relationship_audit(settings, args.run_id, audits)}")
        return
    if args.command == "apply-review":
        settings = Settings()
        run_dir = settings.processed_dir / "runs" / args.run_id
        relationships = read_jsonl(run_dir / "canonical_relationships.jsonl")
        if not relationships:
            parser.error("This run needs canonical_relationships.jsonl.")
        decisions = read_review_csv(Path(args.review_csv))
        reviewed = apply_human_reviews(relationships, decisions)
        write_jsonl(run_dir / "canonical_relationships_reviewed.jsonl", reviewed)
        write_json(run_dir / "human_review_import.json", {"source_csv": str(Path(args.review_csv)), "rows": len(decisions)})
        paths = [
            settings.reports_dir / "runs" / args.run_id / "dashboard-data.json",
            Path("frontend/public/data/runs") / args.run_id / "dashboard-data.json",
        ]
        updated = [publish_human_review_to_dashboard(path, reviewed) for path in paths]
        sync_relationship_reviews_to_postgres(settings.database_url, args.run_id, reviewed)
        counts = {status: sum(1 for row in reviewed if row["review_status"] == status) for status in VALID_STATUSES}
        print(json.dumps(counts, ensure_ascii=False))
        print(f"dashboard_updated={any(updated)}")
        return
    if args.command == "enrich-relationships":
        settings = Settings()
        run_dir = settings.processed_dir / "runs" / args.run_id
        relationships = read_jsonl(run_dir / "canonical_relationships.jsonl")
        audit_path = run_dir / "canonical_relationship_audit.json"
        if not relationships or not audit_path.exists():
            parser.error("Run audit-relationships first; canonical relationships and audit results are required.")
        audits = json.loads(audit_path.read_text(encoding="utf-8")).get("rows", [])
        paths = [settings.reports_dir / "runs" / args.run_id / "dashboard-data.json", Path("frontend/public/data/runs") / args.run_id / "dashboard-data.json"]
        updated = [attach_enrichment_to_dashboard(path, relationships, audits) for path in paths]
        print(f"dashboard_updated={any(updated)}")
        return
    if args.command == "refresh-canonical":
        settings = Settings()
        ensure_dirs(settings)
        run_dir = settings.processed_dir / "runs" / args.run_id
        evidence = [RelationEvidence(**row) for row in read_jsonl(run_dir / "relation_evidence.jsonl")]
        # Migrate legacy artifacts in place: passage IDs remain source-span
        # references, while each assertion gets a deterministic evidence ID.
        write_jsonl(run_dir / "relation_evidence.jsonl", [row.to_dict() for row in evidence])
        raw_evidence_path = run_dir / "relation_evidence_raw.jsonl"
        if raw_evidence_path.exists():
            raw_evidence = [RelationEvidence(**row) for row in read_jsonl(raw_evidence_path)]
            write_jsonl(raw_evidence_path, [row.to_dict() for row in raw_evidence])
        accepted_mappings = {str(row.get("mention_text", "")).casefold(): str(row.get("canonical_name", "")) for row in read_jsonl(run_dir / "entity_resolution_accepted_mappings.jsonl") if row.get("canonical_name")}
        if accepted_mappings:
            evidence = [replace(row, object=accepted_mappings.get(row.object.casefold(), row.object)) for row in evidence]
        companies = read_universe(run_dir / "company_universe_resolved.csv")
        if not evidence or not companies:
            parser.error("This run needs relation_evidence.jsonl and company_universe_resolved.csv.")
        # Only explicit human decisions may bridge a changed relationship id.
        # LLM/cross-filing outcomes are reapplied from their own audit ledger;
        # inheriting them by endpoints can incorrectly confirm a reversed edge.
        prior_reviewed = [
            row for row in (read_jsonl(run_dir / "canonical_relationships_reviewed.jsonl") or read_jsonl(run_dir / "canonical_relationships.jsonl"))
            if row.get("decision_source") == "human_review"
        ]
        entities, relationships, diagnostics = build_canonical_layer(companies, evidence)
        if prior_reviewed:
            relationships = inherit_prior_reviews(relationships, prior_reviewed)
        # A canonical rebuild must never demote a previously audited conclusion.
        # Prefer the shared audit ledger because old local JSON snapshots can be
        # incomplete after a partial audit run.
        local_audit_path = run_dir / "canonical_relationship_audit.json"
        local_audits = json.loads(local_audit_path.read_text(encoding="utf-8")).get("rows", []) if local_audit_path.exists() else []
        durable_audits = load_relationship_audits_from_postgres(settings.database_url, args.run_id)
        # The database is the shared ledger when available, but local audit
        # artifacts make an exported run self-contained and reproducible.
        audits_by_id = {str(row.get("relationship_id", "")): row for row in local_audits}
        audits_by_id.update({str(row.get("relationship_id", "")): row for row in durable_audits})
        durable_audits = list(audits_by_id.values())
        if durable_audits:
            relationships = apply_latest_audit_decisions(relationships, durable_audits)
        # Direction corrections are derived audit artifacts, not raw extractor
        # output. Reattach their durable proposal ledger after rebuilding raw
        # canonical candidates, without allowing a duplicate id.
        correction_path = run_dir / "direction_correction_proposals.jsonl"
        corrections = migrate_relationship_evidence_ids(read_jsonl(correction_path), [row.to_dict() for row in evidence])
        if corrections:
            write_jsonl(correction_path, corrections)
        existing_ids = {str(row.get("relationship_id", "")) for row in relationships}
        relationships.extend(row for row in corrections if str(row.get("relationship_id", "")) not in existing_ids)
        if durable_audits and corrections:
            relationships = apply_latest_audit_decisions(relationships, durable_audits)
        write_jsonl(run_dir / "canonical_entities.jsonl", entities)
        write_jsonl(run_dir / "canonical_relationships.jsonl", relationships)
        write_jsonl(run_dir / "canonical_relationships_reviewed.jsonl", relationships)
        audit_ids = {str(row.get("relationship_id", "")) for row in durable_audits}
        replayed_audit_rows = [row for row in relationships if str(row.get("relationship_id", "")) in audit_ids]
        lineage_current = relationship_lineage_events(relationships, "canonical_refreshed")
        if replayed_audit_rows:
            lineage_current.extend(relationship_lineage_events(replayed_audit_rows, "audit_ledger_replayed"))
        lineage = merge_lineage_history(read_jsonl(run_dir / "relationship_lineage_events.jsonl"), lineage_current)
        write_jsonl(run_dir / "relationship_lineage_events.jsonl", lineage)
        write_csv(run_dir / "canonicalization_diagnostics.csv", diagnostics)
        write_csv(run_dir / "relationship_review_queue.csv", relationship_review_queue(relationships))
        if args.write_postgres:
            publish_relationship_projection(settings, args.run_id, entities, relationships, lineage)
        updated = refresh_canonical_dashboard(settings, args.run_id, entities, relationships, diagnostics)
        accepted = sum(1 for row in relationships if row.get("review_status") == "accepted")
        print(json.dumps({"relationships": len(relationships), "accepted": accepted, "diagnostics": len(diagnostics)}, ensure_ascii=False))
        print(f"dashboard_updated={updated}")
        return
    if args.command == "refresh-mentions":
        settings = Settings()
        ensure_dirs(settings)
        run_dir = settings.processed_dir / "runs" / args.run_id
        passages = [Passage(**row) for row in read_jsonl(run_dir / "passages.jsonl")]
        companies = read_universe(run_dir / "company_universe_resolved.csv")
        if not passages or not companies:
            parser.error("This run needs passages.jsonl and company_universe_resolved.csv.")
        mentions = extract_passage_mentions(passages, companies)
        clusters = build_mention_clusters(mentions)
        passages_by_id = {row.passage_id: row for row in passages}
        triage = build_entity_triage(mentions, clusters, passages_by_id)
        alias_queue = build_alias_review_queue(mentions, clusters, passages_by_id)
        evidence = [RelationEvidence(**row) for row in read_jsonl(run_dir / "relation_evidence.jsonl")]
        resolution_records = build_resolution_records(mentions, clusters, passages, evidence)
        resolution_records = attach_internal_resolution_candidates(
            resolution_records,
            read_jsonl(run_dir / "canonical_entities.jsonl"),
            read_jsonl(run_dir / "entity_resolution_accepted_mappings.jsonl"),
        )
        write_jsonl(run_dir / "entity_mentions.jsonl", [row.to_dict() for row in mentions])
        write_jsonl(run_dir / "mention_clusters.jsonl", [row.to_dict() for row in clusters])
        write_csv(run_dir / "entity_triage.csv", triage)
        write_csv(run_dir / "alias_review_queue.csv", alias_queue)
        write_jsonl(run_dir / "entity_resolution_records.jsonl", resolution_records)
        write_csv(run_dir / "entity_resolution_review.csv", resolution_records)
        if args.write_postgres:
            sync_mention_layer_to_postgres(settings.database_url, args.run_id, mentions, clusters)
            sync_entity_resolution_records_to_postgres(settings.database_url, args.run_id, resolution_records)
        # Resolution Review is served as Vite static data, so keep it in lockstep
        # with the refreshed local/shared record without requiring a full rerun.
        sync_frontend_public_data(settings, read_run_registry(settings.reports_dir / "runs.json"))
        print(json.dumps({"mentions": len(mentions), "clusters": len(clusters), "alias_review_candidates": len(alias_queue), "resolution_records": len(resolution_records), "postgres_synced": args.write_postgres}, ensure_ascii=False))
        return
    if args.command == "reextract-relationships":
        settings = Settings()
        concurrency = args.llm_concurrency or (1 if args.extractor == "rules" else settings.llm_concurrency)
        if not 1 <= concurrency <= MAX_LLM_CONCURRENCY:
            parser.error(f"--llm-concurrency must be between 1 and {MAX_LLM_CONCURRENCY}")
        try:
            preview = create_reextraction_preview(
                settings,
                args.run_id,
                extractor_name=args.extractor,
                llm_concurrency=concurrency,
                preview_id=args.preview_id,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps({"preview_id": preview.preview_id, "preview_dir": str(preview.preview_dir), **preview.summary}, ensure_ascii=False, indent=2))
        return
    if args.command == "preview-mention-constraints":
        settings = Settings()
        run_dir = settings.processed_dir / "runs" / args.run_id
        mentions = [EntityMention(**row) for row in read_jsonl(run_dir / "entity_mentions.jsonl")]
        evidence = [RelationEvidence(**row) for row in read_jsonl(run_dir / "relation_evidence.jsonl")]
        if not mentions or not evidence:
            parser.error("This run needs entity_mentions.jsonl and relation_evidence.jsonl. Run refresh-mentions first.")
        by_passage: dict[str, list[EntityMention]] = {}
        for mention in mentions:
            by_passage.setdefault(mention.passage_id, []).append(mention)
        kept, diagnostics = constrain_relation_records(evidence, by_passage)
        print(json.dumps({"input_evidence": len(evidence), "grounded_evidence": len(kept), "unmentioned_named_dropped": len(diagnostics), "examples": diagnostics[:10]}, ensure_ascii=False, indent=2))
        return
    if args.command == "resolve-entity-records":
        settings = Settings()
        run_dir = settings.processed_dir / "runs" / args.run_id
        records = read_jsonl(run_dir / "entity_resolution_records.jsonl")
        targets = [row for row in records if row.get("resolution_status") == "candidate"]
        targets.sort(key=lambda row: -float(row.get("priority_score", 0)))
        targets = targets[:args.limit] if args.limit > 0 else targets
        if not targets:
            parser.error("No candidate resolution records found. Run refresh-mentions first.")
        contexts = [EntityObjectContext(object=row["mention_text"], evidence_count=int(row["evidence_count"]), subject_count=int(row["distinct_issuer_count"]), subjects="; ".join(row["issuer_names"]), relation_types="", modalities="", forms="", sample_evidence=row["sample_evidence"]) for row in targets]
        gleif = GLEIFClient(requests_per_second=settings.gleif_rps, proxies=settings.proxies)
        candidates = resolve_object_contexts(contexts, gleif, max_candidates=5, include_relationships=True)
        write_candidate_queue(run_dir, candidates, prefix="entity_resolution_candidates")
        concurrency = args.llm_concurrency or settings.llm_concurrency
        if not 1 <= concurrency <= MAX_LLM_CONCURRENCY:
            parser.error(f"--llm-concurrency must be between 1 and {MAX_LLM_CONCURRENCY}")
        model = args.llm_model or settings.complex_model
        client = OpenAICompatibleClient(LLMConfig(base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=model, report_url="", proxy_url=settings.https_proxy or settings.http_proxy, max_connections=concurrency, max_keepalive_connections=min(8, concurrency)))
        selections = select_best_matches_with_llm(candidates, client, model, concurrency=concurrency)
        write_llm_selection_queue(run_dir, selections)
        decisions = decide_alias_resolutions([row.to_dict() for row in candidates], [row.to_dict() for row in selections])
        decisions_by_name = {row["query_object"]: row for row in decisions}
        resolution_id_by_name = {str(row.get("mention_text", "")): str(row.get("resolution_id", "")) for row in records}
        for decision in decisions:
            decision["resolution_id"] = resolution_id_by_name.get(str(decision.get("query_object", "")), "")
        candidates_by_name: dict[str, list[dict]] = {}
        for candidate in candidates:
            candidates_by_name.setdefault(candidate.query_object, []).append(candidate.to_dict())
        selections_by_name = {row.query_object: row.to_dict() for row in selections}
        for record in records:
            if record["mention_text"] not in decisions_by_name:
                continue
            decision = decisions_by_name[record["mention_text"]]
            # Retain earlier Current Graph / confirmed-alias candidates as
            # provenance alongside this resolver's external candidates.
            record["candidate_entities"] = list(record.get("candidate_entities", [])) + candidates_by_name[record["mention_text"]]
            record["llm_assessments"] = selections_by_name[record["mention_text"]].get("candidate_assessments", [])
            record["safety_validation"] = {"status": decision["safety_validation_status"], "reason": decision["safety_validation_reason"], "candidate_conflict": decision["candidate_conflict"]}
            record["decision"] = decision["decision"]
            record["decision_reason"] = decision["policy_reason"]
        write_jsonl(run_dir / "entity_resolution_records.jsonl", records)
        write_csv(run_dir / "entity_resolution_review.csv", records)
        write_jsonl(run_dir / "entity_resolution_decision_events.jsonl", decisions)
        write_csv(run_dir / "entity_resolution_decision_events.csv", decisions)
        accepted_mappings = [
            {"mention_text": row["query_object"], "canonical_name": row["selected_canonical_name"], "lei": row["selected_lei"], "decision": row["decision"], "policy_version": row["policy_version"], "decision_reason": row["policy_reason"]}
            for row in decisions if row["decision"] == "AUTO_ACCEPT" and row.get("selected_canonical_name")
        ]
        write_jsonl(run_dir / "entity_resolution_accepted_mappings.jsonl", accepted_mappings)
        write_csv(run_dir / "entity_resolution_accepted_mappings.csv", accepted_mappings)
        sync_entity_resolution_records_to_postgres(settings.database_url, args.run_id, records)
        append_entity_resolution_decision_events_to_postgres(settings.database_url, args.run_id, decisions)
        sync_frontend_public_data(settings, read_run_registry(settings.reports_dir / "runs.json"))
        print(json.dumps({"resolved_records": len(targets), "decisions": {key: sum(1 for row in decisions if row["decision"] == key) for key in ["AUTO_ACCEPT", "REVIEW", "KEEP_UNRESOLVED"]}}, ensure_ascii=False))
        return
    if args.command == "inherit-reviews":
        settings = Settings()
        target_dir = settings.processed_dir / "runs" / args.run_id
        relationships = read_jsonl(target_dir / "canonical_relationships.jsonl")
        if not relationships:
            parser.error("Target run needs canonical_relationships.jsonl.")
        prior_rows: list[dict] = []
        for source_run in args.from_run:
            source_dir = settings.processed_dir / "runs" / source_run
            source = read_jsonl(source_dir / "canonical_relationships_reviewed.jsonl") or read_jsonl(source_dir / "canonical_relationships.jsonl")
            if not source:
                parser.error(f"Source run {source_run} has no canonical relationship data.")
            prior_rows.extend(row for row in source if row.get("decision_source") == "human_review")
        reviewed = inherit_prior_reviews(relationships, prior_rows)
        write_jsonl(target_dir / "canonical_relationships.jsonl", reviewed)
        write_jsonl(target_dir / "canonical_relationships_reviewed.jsonl", reviewed)
        paths = [settings.reports_dir / "runs" / args.run_id / "dashboard-data.json", Path("frontend/public/data/runs") / args.run_id / "dashboard-data.json"]
        updated = [publish_human_review_to_dashboard(path, reviewed) for path in paths]
        sync_relationship_reviews_to_postgres(settings.database_url, args.run_id, reviewed)
        print(json.dumps({status: sum(1 for row in reviewed if row.get("review_status") == status) for status in VALID_STATUSES}, ensure_ascii=False))
        print(f"dashboard_updated={any(updated)}")
        return
    if args.command == "evaluate":
        settings = Settings()
        relationships = read_jsonl(settings.processed_dir / "runs" / args.run_id / "canonical_relationships.jsonl")
        if not relationships:
            parser.error("Run needs canonical_relationships.jsonl.")
        result = evaluate_canonical_relationships(relationships, load_gold(Path(args.gold)))
        if args.output:
            write_json(Path(args.output), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return


def read_filtered_universe(args: argparse.Namespace):
    return read_universe(
        Path(args.universe),
        tickers=parse_tickers(args.tickers),
        roles=parse_csv_arg(args.roles),
        max_priority=args.priority,
        limit=args.limit_companies,
    )


def parse_forms(forms: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in forms.split(",") if part.strip())


def publish_relationship_audit(settings: Settings, run_id: str, audits: list[dict]) -> bool:
    paths = [
        settings.reports_dir / "runs" / run_id / "dashboard-data.json",
        Path("frontend/public/data/runs") / run_id / "dashboard-data.json",
    ]
    lineage = read_jsonl(settings.processed_dir / "runs" / run_id / "relationship_lineage_events.jsonl")
    updated = []
    for path in paths:
        changed = attach_audits_to_dashboard(path, audits)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["relationship_lineage_events"] = lineage
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            changed = True
        updated.append(changed)
    return any(updated)


def refresh_canonical_dashboard(
    settings: Settings,
    run_id: str,
    entities: list[dict],
    relationships: list[dict],
    diagnostics: list[dict],
) -> bool:
    """Update only canonical dashboard sections, preserving raw filing artifacts."""
    paths = [
        settings.reports_dir / "runs" / run_id / "dashboard-data.json",
        Path("frontend/public/data/runs") / run_id / "dashboard-data.json",
    ]
    updated = False
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing = {str(row.get("relationship_id", "")): row for row in payload.get("canonical_relationships", [])}
        for relationship in relationships:
            prior = existing.get(str(relationship.get("relationship_id", "")), {})
            # Retain deliberate human actions, while allowing a fresh automatic
            # cross-filing acceptance to replace an old unreviewed candidate.
            if prior.get("human_review") and prior.get("review_status") in {"accepted", "rejected", "needs_review"}:
                relationship["human_review"] = prior["human_review"]
                relationship["review_status"] = prior["review_status"]
        payload["canonical_entities"] = entities
        payload["canonical_relationships"] = relationships
        payload["canonicalization_diagnostics"] = diagnostics
        payload["relationship_lineage_events"] = read_jsonl(settings.processed_dir / "runs" / run_id / "relationship_lineage_events.jsonl")
        payload["network_edges"] = canonical_network_edges(relationships)
        payload.setdefault("summary", {})["canonical_entity_count"] = len(entities)
        payload["summary"]["canonical_relationship_count"] = sum(1 for row in relationships if row.get("review_status") == "accepted")
        payload["summary"]["canonical_relationship_candidate_count"] = len(relationships)
        payload["summary"]["canonicalization_excluded_count"] = sum(1 for row in diagnostics if row.get("status") != "canonicalized")
        payload["summary"]["network_edge_count"] = len(payload["network_edges"])
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        updated = True
    return updated


if __name__ == "__main__":
    main()
