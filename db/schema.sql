CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  run_label TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  options JSONB NOT NULL DEFAULT '{}'::jsonb,
  counts JSONB NOT NULL DEFAULT '{}'::jsonb,
  summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS companies (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  ticker TEXT NOT NULL,
  company_name TEXT NOT NULL,
  role TEXT,
  priority INTEGER,
  notes TEXT,
  cik TEXT,
  exchange TEXT,
  PRIMARY KEY (run_id, ticker)
);

CREATE TABLE IF NOT EXISTS filings (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  accession_number TEXT NOT NULL,
  ticker TEXT,
  cik TEXT,
  company_name TEXT,
  form TEXT,
  filing_date DATE,
  report_date TEXT,
  accepted_timestamp TEXT,
  primary_document TEXT,
  archive_url TEXT,
  primary_document_url TEXT,
  local_path TEXT,
  sha256 TEXT,
  PRIMARY KEY (run_id, accession_number)
);

CREATE TABLE IF NOT EXISTS source_documents (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  document_id TEXT NOT NULL,
  accession_number TEXT,
  ticker TEXT,
  cik TEXT,
  company_name TEXT,
  form TEXT,
  filing_date DATE,
  report_date TEXT,
  accepted_timestamp TEXT,
  archive_url TEXT,
  document TEXT,
  document_type TEXT,
  description TEXT,
  sequence TEXT,
  document_url TEXT,
  local_path TEXT,
  sha256 TEXT,
  is_primary BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY (run_id, document_id)
);

CREATE TABLE IF NOT EXISTS passages (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  passage_id TEXT NOT NULL,
  accession_number TEXT,
  ticker TEXT,
  cik TEXT,
  company_name TEXT,
  form TEXT,
  filing_date DATE,
  source_document_url TEXT,
  source_document TEXT,
  source_document_type TEXT,
  section TEXT,
  paragraph_offset INTEGER,
  text TEXT,
  parser_name TEXT,
  parser_version TEXT,
  relevance_score DOUBLE PRECISION,
  relevance_terms TEXT[],
  is_candidate BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY (run_id, passage_id)
);

-- Mention layer is intentionally independent from canonical entities.  It
-- preserves source spans even when a name is unresolved or later re-mapped.
CREATE TABLE IF NOT EXISTS mention_clusters (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  cluster_id TEXT NOT NULL,
  normalized_key TEXT NOT NULL,
  representative_name TEXT NOT NULL,
  proposed_canonical_name TEXT,
  canonical_entity_id TEXT,
  entity_type TEXT,
  resolution_status TEXT NOT NULL,
  resolver_method TEXT,
  mention_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, cluster_id)
);

CREATE TABLE IF NOT EXISTS entity_mentions (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  mention_id TEXT NOT NULL,
  passage_id TEXT NOT NULL,
  text TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  ticker TEXT,
  cik TEXT,
  confidence DOUBLE PRECISION,
  start_offset INTEGER NOT NULL,
  end_offset INTEGER NOT NULL,
  mention_kind TEXT NOT NULL,
  resolution_status TEXT NOT NULL,
  resolver_method TEXT,
  cluster_id TEXT,
  canonical_entity_id TEXT,
  PRIMARY KEY (run_id, mention_id)
);

-- Entity-resolution decisions are separate from canonical entities. A mapping
-- becomes graph input only through an explicit later canonical refresh.
CREATE TABLE IF NOT EXISTS entity_resolution_records (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  resolution_id TEXT NOT NULL,
  record JSONB NOT NULL,
  resolution_status TEXT NOT NULL,
  decision TEXT NOT NULL,
  priority_score DOUBLE PRECISION NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, resolution_id)
);

CREATE TABLE IF NOT EXISTS entity_resolution_decision_events (
  event_id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  resolution_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  decision_source TEXT NOT NULL,
  decision_reason TEXT NOT NULL,
  event JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS relation_evidence (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  evidence_id BIGSERIAL PRIMARY KEY,
  subject TEXT NOT NULL,
  object TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  direction TEXT,
  modality TEXT,
  certainty TEXT,
  temporal_scope TEXT,
  evidence_text TEXT,
  confidence_score DOUBLE PRECISION,
  extractor_model_version TEXT,
  ticker TEXT,
  cik TEXT,
  form TEXT,
  filing_date DATE,
  accepted_timestamp TEXT,
  accession_number TEXT,
  source_document_url TEXT,
  source_section TEXT,
  passage_id TEXT,
  paragraph_offset INTEGER,
  parser_name TEXT,
  parser_version TEXT,
  source_document TEXT,
  source_document_type TEXT
);

CREATE TABLE IF NOT EXISTS graph_edges (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  object TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  modality TEXT NOT NULL,
  first_seen DATE,
  last_seen DATE,
  evidence_count INTEGER,
  avg_confidence DOUBLE PRECISION,
  forms TEXT,
  accessions TEXT,
  source_urls TEXT,
  PRIMARY KEY (run_id, subject, object, relation_type, modality)
);

-- Canonical graph is shared separately from raw extractor output. This keeps
-- the review decision, provenance and supply attributes queryable by the team.
CREATE TABLE IF NOT EXISTS canonical_entities (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  entity_id TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  ticker TEXT,
  cik TEXT,
  role TEXT,
  entity_kind TEXT,
  resolution_status TEXT,
  parent_entity_id TEXT,
  parent_name TEXT,
  PRIMARY KEY (run_id, entity_id)
);

CREATE TABLE IF NOT EXISTS canonical_relationships (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  relationship_id TEXT NOT NULL,
  supplier_entity_id TEXT,
  supplier_name TEXT NOT NULL,
  customer_entity_id TEXT,
  customer_name TEXT NOT NULL,
  source_entity_id TEXT,
  source_entity_name TEXT,
  source_role TEXT,
  target_entity_id TEXT,
  target_entity_name TEXT,
  target_role TEXT,
  relationship_type TEXT NOT NULL,
  relationship_family TEXT NOT NULL,
  product_or_service TEXT,
  categories JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_relation_types JSONB NOT NULL DEFAULT '[]'::jsonb,
  modality TEXT,
  confidence DOUBLE PRECISION,
  evidence_count INTEGER,
  evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  issuer_names JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_accession_numbers JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_types JSONB NOT NULL DEFAULT '[]'::jsonb,
  first_observed_date DATE,
  last_observed_date DATE,
  verification_status TEXT,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',
  decision TEXT,
  decision_source TEXT,
  decision_reason TEXT,
  human_review JSONB,
  risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
  is_active BOOLEAN NOT NULL DEFAULT true,
  PRIMARY KEY (run_id, relationship_id)
);

CREATE TABLE IF NOT EXISTS relationship_audits (
  run_id TEXT NOT NULL,
  relationship_id TEXT NOT NULL,
  audit JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, relationship_id),
  FOREIGN KEY (run_id, relationship_id)
    REFERENCES canonical_relationships(run_id, relationship_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS relationship_lineage_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  relationship_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  event JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- User questions never overwrite an audit or a canonical decision.  A model may
-- flag a connection for later review, but the original fact remains intact.
CREATE TABLE IF NOT EXISTS relationship_challenges (
  challenge_id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL,
  relationship_id TEXT NOT NULL,
  question TEXT NOT NULL,
  response JSONB NOT NULL,
  needs_reaudit BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (run_id, relationship_id)
    REFERENCES canonical_relationships(run_id, relationship_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_relation_evidence_run_subject ON relation_evidence(run_id, subject);
CREATE INDEX IF NOT EXISTS idx_relation_evidence_run_object ON relation_evidence(run_id, object);
CREATE INDEX IF NOT EXISTS idx_relation_evidence_run_type ON relation_evidence(run_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_run_object ON graph_edges(run_id, object);
CREATE INDEX IF NOT EXISTS idx_passages_run_accession ON passages(run_id, accession_number);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_run_passage ON entity_mentions(run_id, passage_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_run_cluster ON entity_mentions(run_id, cluster_id);
CREATE INDEX IF NOT EXISTS idx_entity_resolution_records_run_decision ON entity_resolution_records(run_id, decision);
CREATE INDEX IF NOT EXISTS idx_source_documents_run_accession ON source_documents(run_id, accession_number);
CREATE INDEX IF NOT EXISTS idx_canonical_relationships_run_family ON canonical_relationships(run_id, relationship_family);
CREATE INDEX IF NOT EXISTS idx_canonical_relationships_run_decision ON canonical_relationships(run_id, review_status);
CREATE INDEX IF NOT EXISTS idx_relationship_lineage_run_relationship ON relationship_lineage_events(run_id, relationship_id);
CREATE INDEX IF NOT EXISTS idx_relationship_challenges_run_relationship ON relationship_challenges(run_id, relationship_id);

ALTER TABLE passages ADD COLUMN IF NOT EXISTS source_document_url TEXT;
ALTER TABLE passages ADD COLUMN IF NOT EXISTS source_document TEXT;
ALTER TABLE passages ADD COLUMN IF NOT EXISTS source_document_type TEXT;
ALTER TABLE relation_evidence ADD COLUMN IF NOT EXISTS source_document TEXT;
ALTER TABLE relation_evidence ADD COLUMN IF NOT EXISTS source_document_type TEXT;
ALTER TABLE canonical_relationships ADD COLUMN IF NOT EXISTS source_entity_id TEXT;
ALTER TABLE canonical_relationships ADD COLUMN IF NOT EXISTS source_entity_name TEXT;
ALTER TABLE canonical_relationships ADD COLUMN IF NOT EXISTS source_role TEXT;
ALTER TABLE canonical_relationships ADD COLUMN IF NOT EXISTS target_entity_id TEXT;
ALTER TABLE canonical_relationships ADD COLUMN IF NOT EXISTS target_entity_name TEXT;
ALTER TABLE canonical_relationships ADD COLUMN IF NOT EXISTS target_role TEXT;
ALTER TABLE canonical_relationships ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE canonical_relationships ADD COLUMN IF NOT EXISTS risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb;
