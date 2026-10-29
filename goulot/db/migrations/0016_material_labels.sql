-- 29 October 2026: names of a material in German, Spanish and Italian (Wikidata labels), for the rule filter of the
-- European media feeds. NULL: not asked yet; '{}': Wikidata has none. Public data only.
ALTER TABLE materials ADD COLUMN labels TEXT;
