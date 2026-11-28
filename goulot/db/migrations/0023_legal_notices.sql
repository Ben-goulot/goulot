-- 28 November 2026: legal notices of French companies (BODACC, collective proceedings), matched on the Mac with the
-- suppliers of Ma chaîne (docs/SPEC.md, section 11, « Annonces légales des fournisseurs »).

-- The whole bulletin of collective proceedings, as the State publishes it every working day: public, kept in clear.
-- It is read in full, never searched for a supplier: its presence in the base says nothing of the user.
CREATE TABLE legal_notices (
    id TEXT PRIMARY KEY,                 -- the bulletin's own identifier (« A202602283430 »)
    published TEXT NOT NULL,             -- day of publication (YYYY-MM-DD): deadlines run from it
    kind TEXT NOT NULL,                  -- category computed by the code: safeguard | receivership | liquidation |
                                         -- sale | plan | plan_failed | closed | claims | other
    notice_type TEXT NOT NULL DEFAULT '', -- annonce | rectificatif | annulation
    nature TEXT NOT NULL DEFAULT '',     -- the judgment's own wording
    judgment_date TEXT,
    detail TEXT NOT NULL DEFAULT '',     -- complement of the judgment (cessation of payments, administrator…)
    siren TEXT NOT NULL DEFAULT '',      -- nine digits; '' for a sole trader without one
    name TEXT NOT NULL DEFAULT '',
    legal_form TEXT NOT NULL DEFAULT '',
    activity TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    postcode TEXT NOT NULL DEFAULT '',
    tribunal TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);
CREATE INDEX legal_notices_siren ON legal_notices (siren, published);
CREATE INDEX legal_notices_published ON legal_notices (published);

-- Days of the bulletin already read in full, so that a day is asked once and the past is filled in little by little.
CREATE TABLE legal_days (
    day TEXT PRIMARY KEY,
    notices INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);

-- What the user decided about a match, and which notices were already announced: one encrypted row per Ma chaîne.
-- A notice identifier next to a private record in clear would name the supplier.
CREATE TABLE private_legal (
    id TEXT PRIMARY KEY,                 -- UUID of the row, stable per material
    payload BLOB NOT NULL,               -- {material_id, dismissed: {site_id: [notice ids]}, announced: [notice ids]}
    key_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
