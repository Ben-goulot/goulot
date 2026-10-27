-- 27 October 2026: alert zones (docs/SPEC.md, section 8, « Zones d'alerte »). Public data only.
-- Daily transits of the IMF PortWatch chokepoints, kept to compare the last week with the usual traffic.
CREATE TABLE chokepoint_traffic (
    portid TEXT NOT NULL,
    portname TEXT NOT NULL,
    day TEXT NOT NULL,                   -- YYYY-MM-DD
    n_total REAL NOT NULL,
    PRIMARY KEY (portid, day)
);
-- World hot spots, shown on every map. They never feed a verdict by themselves.
CREATE TABLE hotspots (
    id TEXT PRIMARY KEY,                 -- kind:key
    kind TEXT NOT NULL CHECK (kind IN ('strait_traffic', 'strait_news', 'gdacs', 'copernicus')),
    label TEXT NOT NULL,                 -- external text, shown as text
    status TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    rings TEXT NOT NULL,                 -- JSON list of closed [lon, lat] rings
    source_url TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
