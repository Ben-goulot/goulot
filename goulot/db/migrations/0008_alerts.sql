-- Phase 3, lot 7: alerts sent and weekly digests. No private data (docs/SPEC.md, section 9).

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                  -- verdict, event, budget, silent_source, unverified, security, digest, test
    material_id TEXT,
    dedupe_key TEXT NOT NULL,            -- one alert per key (e.g. budget-80-2026-10)
    text TEXT NOT NULL,                  -- public, neutralized text as sent
    created_at TEXT NOT NULL,
    sent_at TEXT,
    channel TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX alerts_dedupe ON alerts (dedupe_key);

CREATE TABLE digests (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    week TEXT NOT NULL,                  -- ISO week, e.g. 2026-W43
    custom_id TEXT UNIQUE,
    batch_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'done', 'rejected', 'skipped')),
    result TEXT,                         -- validated JSON written by the model
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE (material_id, week)
);
