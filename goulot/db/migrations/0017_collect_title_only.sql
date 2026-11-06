-- 4 November 2026: news items set aside for carrying a headline only (no text, no summary of the source's own).
-- Counted apart, so that the diagnostic tells them from the items the rules rejected.
ALTER TABLE collect_runs ADD COLUMN title_only INTEGER NOT NULL DEFAULT 0;
