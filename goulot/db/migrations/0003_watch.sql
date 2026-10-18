-- Phase 3, lot 1: watch rules, collected items, collection runs. Public data only.
-- Rules from Ma chaîne are never stored (docs/SPEC.md, section 3, step 7).

CREATE TABLE watch_rules (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    kind TEXT NOT NULL CHECK (kind IN ('keyword', 'zone', 'country', 'hs_code', 'strait')),
    value TEXT NOT NULL,                 -- keyword text, ISO2, HS code, strait name; zone: node key
    node_key TEXT,                       -- chain node the rule comes from, when there is one
    lat REAL,
    lon REAL,
    radius_km REAL,
    lang TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX watch_rules_material ON watch_rules (material_id) WHERE active = 1;

-- Articles and hazard or logistics items that passed the rule filter. External text is stored cleaned
-- (nh3, text only) and always displayed as text.
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,                -- source id from config/sources.yaml
    kind TEXT NOT NULL CHECK (kind IN ('news', 'hazard', 'logistics', 'policy')),
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL UNIQUE,       -- SHA-256 of the canonical URL (or of the item key for feeds)
    domain TEXT NOT NULL DEFAULT '',
    provider_domain TEXT NOT NULL DEFAULT '', -- publisher given by a syndication platform's structured data
    title TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    magnitude REAL,                      -- hazard: magnitude, alert level, river level…
    details TEXT NOT NULL DEFAULT '{}',  -- JSON, public fields of the item
    rules_hit TEXT NOT NULL DEFAULT '[]',-- JSON list of watch_rules ids
    materials TEXT NOT NULL DEFAULT '[]',-- JSON list of material ids concerned
    lineage_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'triaged', 'irrelevant', 'grouped', 'quarantined'))
);
CREATE INDEX articles_status ON articles (status, first_seen_at);

-- One row per source per cycle (freshness of the watch: docs/SPEC.md, section 5).
CREATE TABLE collect_runs (
    id INTEGER PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    source TEXT NOT NULL,
    family TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',      -- error type only, never a URL (keys can sit in URLs)
    fetched INTEGER NOT NULL DEFAULT 0,
    kept INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX collect_runs_source ON collect_runs (source, started_at);

CREATE TABLE cycles (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0,       -- news and hazard families each had at least one source answering
    summary TEXT NOT NULL DEFAULT '{}'
);
