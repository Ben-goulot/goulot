-- Phase 3: an impact analysis is answered in several partial calls (the whole schema is too large for the API to
-- compile, 2026-10-18). Each part is kept here until the code merges them into one checked assessment.

CREATE TABLE analysis_parts (
    analysis_id TEXT NOT NULL REFERENCES analyses(id),
    part TEXT NOT NULL,
    custom_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'done', 'error')),
    result TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (analysis_id, part)
);
