-- Phase 3, lot 2: AI triage, batches, events, their articles and links to chain nodes. Public data only.

CREATE TABLE llm_batches (
    id TEXT PRIMARY KEY,                 -- Anthropic batch id (or replay id in tests)
    purpose TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('submitted', 'ended', 'collected', 'failed')),
    request_count INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    collected_at TEXT
);

CREATE TABLE triage (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    custom_id TEXT NOT NULL UNIQUE,
    batch_id TEXT,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'done', 'rejected', 'quarantined', 'error')),
    result TEXT,                         -- validated JSON output
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE INDEX triage_state ON triage (state);

CREATE TABLE events (
    id TEXT PRIMARY KEY,                 -- EVT-YYYY-MM-DD-NNNN
    material_id TEXT NOT NULL REFERENCES materials(id),
    event_type TEXT NOT NULL,
    event_status TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('news', 'hazard', 'logistics', 'policy')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    place TEXT NOT NULL DEFAULT '',
    country_iso2 TEXT NOT NULL DEFAULT 'unknown',
    lat REAL,
    lon REAL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    c_fait REAL NOT NULL DEFAULT 0,
    lineages INTEGER NOT NULL DEFAULT 0,
    best_lineage_reliability REAL NOT NULL DEFAULT 0,
    official_source INTEGER NOT NULL DEFAULT 0,
    confirmation TEXT NOT NULL CHECK (confirmation IN ('confirmed', 'early_signal')),
    severity_hint INTEGER NOT NULL DEFAULT 0,
    future_dates TEXT NOT NULL DEFAULT '[]',
    analysis_state TEXT NOT NULL DEFAULT 'none',  -- none, queued, done, quarantined, skipped
    updated_at TEXT NOT NULL
);
CREATE INDEX events_material ON events (material_id, last_seen_at);

CREATE TABLE event_articles (
    event_id TEXT NOT NULL REFERENCES events(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    lineage_key TEXT NOT NULL,
    reliability REAL NOT NULL,
    PRIMARY KEY (event_id, article_id)
);

CREATE TABLE event_links (
    event_id TEXT NOT NULL REFERENCES events(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    node_key TEXT NOT NULL,
    supply_share_pct REAL,               -- share of world supply exposed through this node, when known
    PRIMARY KEY (event_id, node_key)
);
