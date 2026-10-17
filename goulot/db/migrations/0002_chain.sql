-- Phase 2: materials, typical chain (public data, not encrypted), sources, AI calls and reconstruction jobs.
-- Additive only (docs/UPDATES.md). Nothing private here: Ma chaîne arrives in phase 4, encrypted.

CREATE TABLE materials (
    id TEXT PRIMARY KEY,                 -- UUID
    name TEXT NOT NULL,
    cas TEXT NOT NULL UNIQUE,
    formula TEXT NOT NULL DEFAULT '',
    synonyms TEXT NOT NULL DEFAULT '[]', -- JSON list
    hs_codes TEXT NOT NULL DEFAULT '[]', -- JSON list of {code, origin: ecics|ai, verified: bool}
    identity_sources TEXT NOT NULL DEFAULT '[]', -- JSON list of {name, url}
    status TEXT NOT NULL CHECK (status IN ('draft', 'reconstructing', 'review', 'validated', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    validated_at TEXT
);

-- Bitemporal: a correction closes the current row (valid_to) and inserts a new one.
CREATE TABLE chain_nodes (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    node_key TEXT NOT NULL,
    node_type TEXT NOT NULL,
    name TEXT NOT NULL,
    product TEXT NOT NULL,
    tier INTEGER NOT NULL,
    country_iso2 TEXT NOT NULL,
    place TEXT NOT NULL,
    lat REAL,
    lon REAL,
    global_share_pct REAL,
    buffer_days REAL,
    buffer_is_default INTEGER NOT NULL DEFAULT 0,
    capacity_text TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    notes TEXT NOT NULL DEFAULT '',
    anomalies TEXT NOT NULL DEFAULT '[]',  -- JSON list of French messages shown on the Chaîne screen
    user_modified INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX chain_nodes_current ON chain_nodes (material_id, node_key) WHERE valid_to IS NULL;

CREATE TABLE chain_edges (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    from_key TEXT NOT NULL,
    to_key TEXT NOT NULL,
    relation TEXT NOT NULL,
    transport_mode TEXT NOT NULL,
    lead_time_days REAL,
    lead_time_basis TEXT NOT NULL DEFAULT 'source', -- source | sea_estimate | default | user
    share_pct REAL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    anomalies TEXT NOT NULL DEFAULT '[]',
    user_modified INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX chain_edges_current ON chain_edges (material_id) WHERE valid_to IS NULL;

CREATE TABLE chain_routes (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    route_key TEXT NOT NULL,
    name TEXT NOT NULL,
    global_share_pct REAL,
    description TEXT NOT NULL,
    node_keys TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    recorded_at TEXT NOT NULL
);

CREATE TABLE chain_risks (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    kind TEXT NOT NULL CHECK (kind IN ('risk', 'substitute', 'open_question')),
    risk_type TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    node_keys TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    recorded_at TEXT NOT NULL
);

CREATE TABLE chain_dates (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    date TEXT NOT NULL,
    what TEXT NOT NULL,
    node_keys TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    recorded_at TEXT NOT NULL
);

-- URLs come only from web search citations or from the identification sources, never from a model.
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    ref TEXT NOT NULL,                   -- src1, src2… as shown to the structuring call
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    page_age TEXT NOT NULL DEFAULT '',
    retrieved_at TEXT NOT NULL,
    UNIQUE (material_id, ref)
);

CREATE TABLE node_sources (
    node_id INTEGER NOT NULL REFERENCES chain_nodes(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    PRIMARY KEY (node_id, source_id)
);

CREATE TABLE edge_sources (
    edge_id INTEGER NOT NULL REFERENCES chain_edges(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    PRIMARY KEY (edge_id, source_id)
);

-- Research report with [srcN] markers (public data), kept for the "Voir les sources" panel and re-structuring.
CREATE TABLE chain_reports (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    report TEXT NOT NULL,
    injection_suspected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Every AI call: billed tokens and cost; request id, never the content (CLAUDE.md, AI calls).
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    budget TEXT NOT NULL CHECK (budget IN ('watch', 'reconstruction', 'dev')),
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('standard', 'batch')),
    request_id TEXT NOT NULL DEFAULT '',
    material_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    web_searches INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    stop_reason TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX llm_calls_month ON llm_calls (budget, at);

-- A quote shown to the user, then confirmed: reconstruction cannot start without it.
CREATE TABLE quotes (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    kind TEXT NOT NULL,
    estimate_usd REAL NOT NULL,
    max_usd REAL NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    kind TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,     -- 1 identification … 5 délais
    state TEXT NOT NULL CHECK (state IN ('running', 'done', 'failed')),
    message TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
