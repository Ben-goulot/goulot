-- Phase 3 (history): official Chinese prices already read, so that no page is fetched twice.
-- nbs_releases: ten-day releases of the National Bureau of Statistics (goulot/prices/nbs.py).
-- exchange_days: daily files of the Chinese exchanges, days without trading included (goulot/prices/shfe.py).
-- Their prices go to the `prices` table like any other series.

CREATE TABLE nbs_releases (
    url TEXT PRIMARY KEY,
    period TEXT NOT NULL,       -- last day covered: the 10th, the 20th or the end of the month
    products INTEGER NOT NULL,  -- products found in the table (50 since 2025)
    fetched_at TEXT NOT NULL
);

CREATE TABLE exchange_days (
    source TEXT NOT NULL,
    day TEXT NOT NULL,
    products INTEGER NOT NULL,  -- 0: no trading that day (holiday)
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, day)
);
