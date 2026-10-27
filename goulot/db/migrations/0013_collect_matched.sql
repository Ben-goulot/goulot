-- 27 October 2026: items that passed the rule filter, new or already stored. With `fetched` and `kept`,
-- it tells rejected items (fetched - matched) from duplicates (matched - kept).
ALTER TABLE collect_runs ADD COLUMN matched INTEGER NOT NULL DEFAULT 0;
