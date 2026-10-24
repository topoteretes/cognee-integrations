-- Ingestion ledger for the Cognee company-memory plugin.
-- One row per (company, source type, source id, revision). The unique
-- constraint is what makes replayed and reordered host events idempotent.
-- Lives in the plugin's own namespace (schema); the host runs this once.

CREATE TABLE IF NOT EXISTS memory_ledger (
  id              TEXT PRIMARY KEY,
  company_id      TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  source_type     TEXT NOT NULL,           -- issue | comment | decision
  source_id       TEXT NOT NULL,
  revision        TEXT NOT NULL,           -- sha256 of the stored record text
  dataset         TEXT NOT NULL,           -- cognee dataset name (opaque, per project)
  upload_name     TEXT NOT NULL,           -- <source_type>-<revision[:32]>: the document's identity on cognee
  content         TEXT,                    -- the stored record text; cleared on retire/revoke
  source_ref      TEXT NOT NULL,
  link            TEXT,
  status          TEXT NOT NULL,           -- pending | accepted | indexing | ready | uncertain | failed | conflict | superseded | retired | revoked
  dataset_id      TEXT,
  data_id         TEXT,
  error           TEXT,
  attempts        INTEGER NOT NULL DEFAULT 0,
  created_at      TIMESTAMPTZ NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL,
  last_checked_at TIMESTAMPTZ,
  CONSTRAINT memory_ledger_source_revision UNIQUE (company_id, source_type, source_id, revision)
);

CREATE INDEX IF NOT EXISTS memory_ledger_status ON memory_ledger (status);
CREATE INDEX IF NOT EXISTS memory_ledger_company_project ON memory_ledger (company_id, project_id);
CREATE INDEX IF NOT EXISTS memory_ledger_source ON memory_ledger (company_id, source_type, source_id);
