-- 27 October 2026: where a node's position comes from, and nodes added by Goulot itself (probable route straits).
-- coord_source: user | ai | reference (strait, waterway) | city | country (approximate: never a zone rule) | none;
-- '' for rows written before this migration, located at the next cycle.
ALTER TABLE chain_nodes ADD COLUMN coord_source TEXT NOT NULL DEFAULT '';
-- added_by: '' (reconstruction or user), 'goulot' (strait on a probable sea route, docs/SPEC.md, section 3).
ALTER TABLE chain_nodes ADD COLUMN added_by TEXT NOT NULL DEFAULT '';
