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

CREATE INDEX IF NOT EXISTS idx_relation_evidence_run_subject ON relation_evidence(run_id, subject);
CREATE INDEX IF NOT EXISTS idx_relation_evidence_run_object ON relation_evidence(run_id, object);
CREATE INDEX IF NOT EXISTS idx_relation_evidence_run_type ON relation_evidence(run_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_run_object ON graph_edges(run_id, object);
CREATE INDEX IF NOT EXISTS idx_passages_run_accession ON passages(run_id, accession_number);
CREATE INDEX IF NOT EXISTS idx_source_documents_run_accession ON source_documents(run_id, accession_number);

ALTER TABLE passages ADD COLUMN IF NOT EXISTS source_document_url TEXT;
ALTER TABLE passages ADD COLUMN IF NOT EXISTS source_document TEXT;
ALTER TABLE passages ADD COLUMN IF NOT EXISTS source_document_type TEXT;
ALTER TABLE relation_evidence ADD COLUMN IF NOT EXISTS source_document TEXT;
ALTER TABLE relation_evidence ADD COLUMN IF NOT EXISTS source_document_type TEXT;
