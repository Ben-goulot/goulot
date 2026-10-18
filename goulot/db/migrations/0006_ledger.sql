-- Phase 3, lot 4: the advice ledger, append-only and hash-chained (CLAUDE.md, rule 12; docs/SPEC.md, section 7).
-- No function and no migration updates or deletes an entry: the triggers below refuse it at the database level.

CREATE TABLE ledger (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,                    -- UTC
    material_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('verdict', 'hypothesis', 'outcome', 'user_feedback', 'signal', 'pressure',
                                       'missed_move', 'settings_change', 'export', 'correction')),
    content TEXT NOT NULL,               -- canonical JSON, public fields only
    private_blob BLOB,                   -- AES-256-GCM ciphertext of the private fields, if any
    private_key_version INTEGER,
    private_digest TEXT NOT NULL DEFAULT '', -- SHA-256 of the ciphertext: the chain is verifiable without the key
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE
);
CREATE INDEX ledger_material ON ledger (material_id, kind, at);

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;

CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;
