-- Phase 3, lot 3: impact analyses, price hypotheses and verdicts.
-- verdicts.inputs is AES-256-GCM ciphertext (docs/SECURITY.md, vault scope); the rest is public.

CREATE TABLE analyses (
    id TEXT PRIMARY KEY,                 -- UUID
    event_id TEXT NOT NULL REFERENCES events(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('standard', 'batch')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'done', 'quarantined', 'rejected', 'error')),
    custom_id TEXT UNIQUE,
    batch_id TEXT,
    signature TEXT NOT NULL,             -- event state analyzed (confirmation, lineages, severity hint, status)
    dossier_sources TEXT NOT NULL DEFAULT '{}',  -- JSON: S… / R… / instrument ids sent, for the business checks
    result TEXT,
    quarantine_reason TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE INDEX analyses_event ON analyses (event_id, created_at);

CREATE TABLE hypotheses (
    id INTEGER PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(id),
    hypothesis_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    direction TEXT NOT NULL,
    horizon TEXT NOT NULL,
    probability REAL NOT NULL,
    move_p10 REAL NOT NULL,
    move_p90 REAL NOT NULL,
    move_unit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    outcome TEXT                         -- filled from the ledger's outcome entries (lot 4)
);

CREATE TABLE verdicts (
    id TEXT PRIMARY KEY,                 -- UUID
    material_id TEXT NOT NULL REFERENCES materials(id),
    scope TEXT NOT NULL CHECK (scope IN ('market', 'personal')),
    verdict TEXT NOT NULL CHECK (verdict IN ('ras', 'pot', 'ant', 'unverified')),
    pressure INTEGER NOT NULL,
    x_weeks REAL,
    x_low REAL,
    x_high REAL,
    window_weeks REAL,
    window_assumed INTEGER NOT NULL DEFAULT 1,
    rules_only INTEGER NOT NULL DEFAULT 0,
    break_in INTEGER NOT NULL DEFAULT 1,  -- « rodage »
    explanation TEXT NOT NULL,
    event_ids TEXT NOT NULL DEFAULT '[]',
    inputs BLOB,                         -- encrypted inputs of the computation (null when the vault is absent)
    inputs_key_version INTEGER,
    inputs_digest TEXT NOT NULL DEFAULT '', -- HMAC-SHA-256 with a vault key
    last_ok_cycle TEXT,
    created_at TEXT NOT NULL,
    current INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX verdicts_current ON verdicts (material_id, scope) WHERE current = 1;
