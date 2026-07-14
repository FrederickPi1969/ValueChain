CREATE TABLE IF NOT EXISTS acquisition_sources (
  source_id TEXT PRIMARY KEY,
  authority TEXT NOT NULL DEFAULT '',
  canonical BOOLEAN NOT NULL DEFAULT true,
  enabled BOOLEAN NOT NULL DEFAULT false,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS acquisition_issuers (
  source_id TEXT NOT NULL REFERENCES acquisition_sources(source_id),
  source_issuer_id TEXT NOT NULL,
  ticker TEXT NOT NULL DEFAULT '',
  company_name TEXT NOT NULL DEFAULT '',
  exchange TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 500,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, source_issuer_id)
);

CREATE TABLE IF NOT EXISTS acquisition_issuer_scans (
  source_id TEXT NOT NULL,
  source_issuer_id TEXT NOT NULL,
  filing_year INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ,
  claimed_at TIMESTAMPTZ,
  scanned_at TIMESTAMPTZ,
  last_error TEXT,
  PRIMARY KEY (source_id, source_issuer_id, filing_year),
  FOREIGN KEY (source_id, source_issuer_id)
    REFERENCES acquisition_issuers(source_id, source_issuer_id)
    ON DELETE CASCADE,
  CHECK (status IN ('pending', 'running', 'retry', 'complete'))
);

CREATE INDEX IF NOT EXISTS idx_acquisition_scan_queue
ON acquisition_issuer_scans(filing_year, status, next_attempt_at, scanned_at);

CREATE INDEX IF NOT EXISTS idx_acquisition_issuer_priority
ON acquisition_issuers(source_id, priority, ticker);

CREATE TABLE IF NOT EXISTS acquisition_universe_snapshots (
  snapshot_id BIGSERIAL PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES acquisition_sources(source_id),
  source_url TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  retrieved_at TIMESTAMPTZ,
  imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL DEFAULT 'complete',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (source_id, sha256)
);

CREATE INDEX IF NOT EXISTS idx_acquisition_universe_snapshots_source
ON acquisition_universe_snapshots(source_id, imported_at DESC);

CREATE TABLE IF NOT EXISTS acquisition_filings (
  source_id TEXT NOT NULL REFERENCES acquisition_sources(source_id),
  source_filing_id TEXT NOT NULL,
  source_issuer_id TEXT NOT NULL,
  form_raw TEXT NOT NULL,
  filing_date DATE NOT NULL,
  report_date TEXT,
  accepted_at TEXT,
  primary_document TEXT,
  archive_url TEXT NOT NULL,
  local_dir TEXT NOT NULL,
  discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL,
  last_error TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (source_id, source_filing_id),
  FOREIGN KEY (source_id, source_issuer_id)
    REFERENCES acquisition_issuers(source_id, source_issuer_id)
);

CREATE INDEX IF NOT EXISTS idx_acquisition_filings_issuer_date
ON acquisition_filings(source_id, source_issuer_id, filing_date);

CREATE INDEX IF NOT EXISTS idx_acquisition_filings_status
ON acquisition_filings(source_id, status, filing_date);

ALTER TABLE acquisition_filings
ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS acquisition_documents (
  document_id BIGSERIAL PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES acquisition_sources(source_id),
  source_filing_id TEXT NOT NULL,
  document_kind TEXT NOT NULL,
  source_url TEXT NOT NULL,
  local_path TEXT NOT NULL,
  content_type TEXT,
  byte_size BIGINT,
  sha256 TEXT,
  retrieved_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  last_error TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (source_id, source_url),
  FOREIGN KEY (source_id, source_filing_id)
    REFERENCES acquisition_filings(source_id, source_filing_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_acquisition_documents_filing
ON acquisition_documents(source_id, source_filing_id);

CREATE INDEX IF NOT EXISTS idx_acquisition_documents_hash
ON acquisition_documents(sha256) WHERE sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_acquisition_documents_status
ON acquisition_documents(source_id, status, retrieved_at);

CREATE TABLE IF NOT EXISTS acquisition_source_checkpoints (
  source_id TEXT NOT NULL REFERENCES acquisition_sources(source_id),
  checkpoint_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  cursor TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  next_attempt_at TIMESTAMPTZ,
  last_error TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (source_id, checkpoint_key)
);

CREATE INDEX IF NOT EXISTS idx_acquisition_source_checkpoints_due
ON acquisition_source_checkpoints(source_id, status, next_attempt_at, completed_at);

CREATE TABLE IF NOT EXISTS acquisition_source_objects (
  source_id TEXT NOT NULL REFERENCES acquisition_sources(source_id),
  object_key TEXT NOT NULL,
  object_type TEXT NOT NULL,
  source_url TEXT NOT NULL,
  local_path TEXT NOT NULL,
  content_type TEXT,
  byte_size BIGINT,
  sha256 TEXT,
  retrieved_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed_at TIMESTAMPTZ,
  next_attempt_at TIMESTAMPTZ,
  last_error TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (source_id, object_key)
);

ALTER TABLE acquisition_source_objects
ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;

ALTER TABLE acquisition_source_objects
ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

ALTER TABLE acquisition_source_objects
ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_acquisition_source_objects_status
ON acquisition_source_objects(source_id, status, retrieved_at);

CREATE INDEX IF NOT EXISTS idx_acquisition_source_objects_queue
ON acquisition_source_objects(source_id, status, next_attempt_at, object_key DESC);

CREATE INDEX IF NOT EXISTS idx_acquisition_source_objects_hash
ON acquisition_source_objects(source_id, sha256) WHERE sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS acquisition_api_usage (
  source_id TEXT NOT NULL,
  usage_date DATE NOT NULL,
  request_count INTEGER NOT NULL DEFAULT 0,
  request_limit INTEGER NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, usage_date),
  CHECK (request_count >= 0),
  CHECK (request_limit > 0),
  CHECK (request_count <= request_limit)
);

CREATE INDEX IF NOT EXISTS idx_acquisition_api_usage_recent
ON acquisition_api_usage(source_id, usage_date DESC);

CREATE TABLE IF NOT EXISTS acquisition_runs (
  run_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES acquisition_sources(source_id),
  target_year INTEGER NOT NULL,
  mode TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  status TEXT NOT NULL,
  issuer_count INTEGER NOT NULL DEFAULT 0,
  filing_count INTEGER NOT NULL DEFAULT 0,
  document_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS acquisition_ad_hoc_requests (
  request_id UUID PRIMARY KEY,
  request_key TEXT NOT NULL UNIQUE,
  source_id TEXT NOT NULL,
  source_issuer_id TEXT NOT NULL,
  requested_year INTEGER NOT NULL,
  canonical_document_type TEXT NOT NULL,
  source_document_type TEXT NOT NULL DEFAULT '',
  year_basis TEXT NOT NULL DEFAULT 'auto',
  include_amendments BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed_at TIMESTAMPTZ,
  next_attempt_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  result_document_ids BIGINT[] NOT NULL DEFAULT '{}'::bigint[],
  request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_code TEXT,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status IN (
    'queued', 'discovering', 'downloading', 'complete', 'not_found',
    'retry', 'failed', 'unsupported'
  )),
  CHECK (requested_year BETWEEN 1994 AND 2100),
  CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_acquisition_ad_hoc_queue
ON acquisition_ad_hoc_requests(status, next_attempt_at, created_at);

CREATE INDEX IF NOT EXISTS idx_acquisition_ad_hoc_lookup
ON acquisition_ad_hoc_requests(
  source_id, source_issuer_id, requested_year, canonical_document_type
);

INSERT INTO acquisition_sources(source_id, authority, canonical, enabled)
VALUES ('sec_edgar', 'U.S. Securities and Exchange Commission', true, true)
ON CONFLICT (source_id) DO UPDATE
SET authority = EXCLUDED.authority,
    canonical = EXCLUDED.canonical,
    enabled = EXCLUDED.enabled,
    updated_at = now();
