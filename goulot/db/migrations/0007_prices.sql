-- Phase 3, lot 5: public price series, their history, and their links to materials (docs/SPEC.md, section 6).

CREATE TABLE instruments (
    id TEXT PRIMARY KEY,                 -- « source:code », e.g. eia:RBRTE, census:292610, bls:WPU0614
    source TEXT NOT NULL,                -- source id of config/sources.yaml
    code TEXT NOT NULL,                  -- series identifier at the source
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 6), -- 1 quote, 2 official price, 3 customs, 4 index, 5 proxy, 6 fallback
    frequency TEXT NOT NULL CHECK (frequency IN ('daily', 'monthly', 'customs', 'tenday')),
    params TEXT NOT NULL DEFAULT '{}',
    last_fetch_at TEXT,
    last_point_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE prices (
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    day TEXT NOT NULL,                   -- YYYY-MM-DD (first day of the month for monthly series)
    value REAL NOT NULL,
    volume REAL,                         -- customs: quantity of the month, for the minimum-volume rule
    PRIMARY KEY (instrument_id, day)
);

CREATE TABLE instrument_links (
    material_id TEXT NOT NULL REFERENCES materials(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    role TEXT NOT NULL CHECK (role IN ('direct', 'official', 'customs', 'index', 'input', 'derivative', 'factor')),
    node_key TEXT,                       -- input: the chain node (direct input) the series prices
    weight REAL NOT NULL DEFAULT 1,
    validated INTEGER NOT NULL DEFAULT 0, -- 1 once the user has checked the link
    proposed_by TEXT NOT NULL CHECK (proposed_by IN ('reconstruction', 'user', 'auto')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (material_id, instrument_id, role)
);
