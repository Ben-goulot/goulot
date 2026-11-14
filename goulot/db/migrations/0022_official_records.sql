-- 14 November 2026: official records followed over time (docs/SPEC.md, section 4, « Registres »).
-- Public data only: declarations to a regulator and the sites of the typical chains. Nothing from Ma chaîne.

-- Every declaration read, kept so that it is read once, followed until its final report, and so that each
-- installation has a past to compare a new declaration with.
CREATE TABLE official_records (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,                -- tceq_emissions
    ext_id TEXT NOT NULL,                -- the registry's own number of the declaration
    ref TEXT NOT NULL DEFAULT '',        -- the registry's own number of the installation (Texas: RN…)
    entity TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    county TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',       -- the registry's own type, upper case
    unit TEXT NOT NULL DEFAULT '',
    cause TEXT NOT NULL DEFAULT '',
    compounds TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    ended_at TEXT,
    notified_at TEXT,                    -- when the registry was told: what anyone could read from then on
    final INTEGER NOT NULL DEFAULT 0,    -- its final report is in
    detailed INTEGER NOT NULL DEFAULT 0, -- 1: its own page was read; 0: known from a list only
    url TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    read_at TEXT NOT NULL,
    emitted TEXT NOT NULL DEFAULT '',    -- what was sent to the watch: '' (routine, nothing) | first | final
    UNIQUE (source, ext_id)
);
CREATE INDEX official_records_ref ON official_records (source, ref, started_at);

-- An installation of a registry recognized as a site of a typical chain. Bound once, by name; then the number
-- alone names the site, whatever the registry calls it later.
CREATE TABLE registry_installations (
    source TEXT NOT NULL,
    ref TEXT NOT NULL,
    material_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    entity TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,               -- name and place of the site when it was bound: a node rebuilt as another
                                         -- site under the same key no longer answers to this installation
    how TEXT NOT NULL,                   -- name | search
    bound_at TEXT NOT NULL,
    history_from TEXT,                   -- its past declarations were read back to that day
    PRIMARY KEY (source, ref, material_id, node_key)
);

-- What a registry can say of each site: never « calm » for a site it does not cover or could not find.
-- status: bound (followed by its number) | none (covered, no installation found under that name) | outside.
CREATE TABLE registry_coverage (
    source TEXT NOT NULL,
    material_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    subject TEXT NOT NULL,               -- name and place the answer was worked out for: asked again when they change
    status TEXT NOT NULL CHECK (status IN ('bound', 'none', 'outside')),
    checked_at TEXT NOT NULL,
    PRIMARY KEY (source, material_id, node_key)
);

-- Who operates a site, from a public registry of plants (United States: EPA Toxics Release Inventory): legal names
-- of the operator, county and state. A subsidiary's name is how regulators write the plant.
CREATE TABLE site_identities (
    material_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    source TEXT NOT NULL,                -- epa_tri
    subject TEXT NOT NULL,
    names TEXT NOT NULL DEFAULT '[]',    -- JSON list of legal names
    county TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (material_id, node_key, source)
);
