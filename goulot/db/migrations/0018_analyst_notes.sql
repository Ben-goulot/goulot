-- 6 November 2026, lot 10: the analyst's reading of each material (docs/SPEC.md, section 4, « Analyste »).
-- Public data only: the dossier is built from whitelisted public fields, never from Ma chaîne.
CREATE TABLE analyst_notes (
    id INTEGER PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    trigger TEXT NOT NULL CHECK (trigger IN ('first', 'weekly', 'verdict', 'event')),
    week TEXT NOT NULL,                  -- ISO week of the digest it feeds, e.g. 2026-W45
    custom_id TEXT NOT NULL UNIQUE,
    batch_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'done', 'rejected', 'error')),
    verdict_id TEXT,                     -- the market verdict it read
    serious TEXT NOT NULL DEFAULT '[]',  -- serious events and analyses it had seen (JSON)
    dossier TEXT NOT NULL,               -- references sent (E…, X… -> event ids), publishers, date (JSON)
    result TEXT,                         -- validated JSON written by the model, when done
    problems TEXT NOT NULL DEFAULT '',   -- why a reading was rejected (codes, no model text)
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE INDEX analyst_notes_material ON analyst_notes (material_id, created_at);
