-- 11 November 2026: a chain that says what its figures are, and that can be brought up to date.
-- status: operating | idled | closed | project | unknown. A closed or project node is kept, shown, never watched.
ALTER TABLE chain_nodes ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown';
-- share_basis: what global_share_pct is a share of (production | capacity | exports | unknown), and its year.
-- Rows written before this migration stay « unknown »: the screen says so instead of guessing.
ALTER TABLE chain_nodes ADD COLUMN share_basis TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE chain_nodes ADD COLUMN share_year INTEGER;

-- Light update of a validated chain (docs/SPEC.md, section 3, « Mise à jour ») : a short sourced report of what
-- changed since the last research, then proposals that the user accepts or rejects one by one. Public data only.
CREATE TABLE chain_updates (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    kind TEXT NOT NULL CHECK (kind IN ('search', 'reread')),  -- new research since `since`, or stored report re-read
    since TEXT NOT NULL,                 -- date of the research this update starts from
    report TEXT NOT NULL DEFAULT '',     -- search only: [srcN] markers; its sources are added to `sources`
    notes TEXT NOT NULL DEFAULT '[]',    -- JSON list of French open questions and dropped proposals
    injection_suspected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX chain_updates_material ON chain_updates (material_id, id);

CREATE TABLE chain_update_items (
    id INTEGER PRIMARY KEY,
    update_id INTEGER NOT NULL REFERENCES chain_updates(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    change_type TEXT NOT NULL CHECK (change_type IN ('status', 'share', 'capacity', 'new_producer', 'date', 'note')),
    node_key TEXT NOT NULL DEFAULT '',   -- existing node, or the key proposed for a new producer
    summary TEXT NOT NULL,               -- one French sentence, shown as text
    payload TEXT NOT NULL DEFAULT '{}',  -- JSON: only the fields this change sets, already checked by code
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    source_refs TEXT NOT NULL DEFAULT '[]',
    anomalies TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'accepted', 'rejected')),
    decided_at TEXT
);
CREATE INDEX chain_update_items_pending ON chain_update_items (material_id, decision);
