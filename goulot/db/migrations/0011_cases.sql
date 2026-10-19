-- Phase 3 (history): library of past cases and the price reactions measured by the code (docs/SPEC.md, section 6).
-- A case prepared by the AI stays a draft until the user verifies it; a draft never enters a median or a dossier.
-- Public data only: the chain type, public sources and public price series. Nothing from Ma chaîne.

CREATE TABLE historical_events (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    case_id TEXT NOT NULL,
    title TEXT NOT NULL,
    event_type TEXT NOT NULL,
    first_report_date TEXT NOT NULL,     -- YYYY-MM-DD, or YYYY-MM when the day is unknown
    end_date TEXT NOT NULL,              -- same formats, or unknown
    place TEXT NOT NULL,
    country_iso2 TEXT NOT NULL,
    node_type TEXT NOT NULL,
    affected TEXT NOT NULL,              -- JSON list of commodity names
    capacity_share_pct REAL,
    capacity_share_basis TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('tightening', 'loosening', 'ambiguous')),
    duration_days REAL,
    summary TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    status TEXT NOT NULL CHECK (status IN ('draft', 'verified', 'rejected')),
    anomalies TEXT NOT NULL DEFAULT '[]', -- JSON list of French messages; a case with one cannot be verified
    job_id TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE (material_id, case_id)
);

CREATE TABLE case_sources (
    case_row INTEGER NOT NULL REFERENCES historical_events(id),
    ref TEXT NOT NULL,                   -- srcN (web search) or leadN (public database of past events)
    url TEXT NOT NULL,                   -- kept by the code, never written by the AI
    title TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    page_age TEXT NOT NULL DEFAULT '',
    reliability REAL NOT NULL,
    PRIMARY KEY (case_row, ref)
);

-- Research reports of the case preparation (public data, [srcN] and [leadN] markers), for the sources panel.
CREATE TABLE case_reports (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    job_id TEXT NOT NULL,
    report TEXT NOT NULL,
    injection_suspected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
