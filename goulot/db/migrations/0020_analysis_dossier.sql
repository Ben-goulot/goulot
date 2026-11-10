-- Two-step impact analysis (decision of 10 November 2026): the dossier sent with the first call is kept until the
-- analysis ends, so that the second step reads exactly the same sources. Public, whitelisted fields only.
ALTER TABLE analyses ADD COLUMN dossier_text TEXT;
