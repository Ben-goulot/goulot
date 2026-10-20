-- Phase 4: Ma chaîne, supplier signals, quotes, purchases, private links and personal verdicts (docs/SPEC.md,
-- sections 2, 5 and 11). Every payload is AES-256-GCM ciphertext bound to a logical field and to the record UUID
-- (goulot/vault/crypto.py); nothing private is written in clear. The material of a record is inside its ciphertext:
-- the plain columns do not even say which material has a Ma chaîne, a signal or a purchase.

-- Ma chaîne: append-only revisions (a change creates a new revision; a personal verdict notes the one it used).
CREATE TABLE private_chain (
    id TEXT PRIMARY KEY,                 -- UUID, bound into the ciphertext
    payload BLOB NOT NULL,               -- {material_id, revision, chain}
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER private_chain_no_update BEFORE UPDATE ON private_chain
BEGIN
    SELECT RAISE(ABORT, 'Ma chaîne revisions are append-only');
END;

CREATE TABLE user_signals (
    id TEXT PRIMARY KEY,
    payload BLOB NOT NULL,               -- {material_id, type, date, effective_date, site_id, values, comment}
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE user_quotes (
    id TEXT PRIMARY KEY,
    payload BLOB NOT NULL,               -- {material_id, date, unit_price, currency, unit, site_id}
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE user_purchases (
    id TEXT PRIMARY KEY,
    payload BLOB NOT NULL,               -- {material_id, date, unit_price, quantity, currency, unit, site_id}
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Links between events and sites of Ma chaîne, recomputed on the Mac at each cycle: one row per Ma chaîne,
-- so that no event identifier is left in clear next to a private record.
CREATE TABLE private_links (
    id TEXT PRIMARY KEY,                 -- UUID of the row, stable per material
    payload BLOB NOT NULL,               -- {material_id, links: {event_id: {sites, how}}}
    key_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

-- Personal verdicts: X, the action window, the explanation and the exposure are encrypted together.
ALTER TABLE verdicts ADD COLUMN private BLOB;
ALTER TABLE verdicts ADD COLUMN private_key_version INTEGER;

-- Public events: linked to a node of the typical chain, or found by a stored public rule. An event found only
-- through Ma chaîne never leaves through the market verdict, the digest, the public alerts or the public routes.
CREATE VIEW public_events AS
SELECT * FROM events e
WHERE EXISTS (SELECT 1 FROM event_links l WHERE l.event_id = e.id)
   OR EXISTS (SELECT 1 FROM event_articles ea JOIN articles a ON a.id = ea.article_id
              WHERE ea.event_id = e.id AND a.rules_hit != '[]');
