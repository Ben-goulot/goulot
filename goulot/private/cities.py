"""Offline place lookup for the sites of Ma chaîne (docs/SPEC.md, section 11): no geocoding service, the names of the
user's suppliers go nowhere.

`goulot/data/cities.json.gz`: [[name, ascii name, [alternate names], iso2, lat, lon, population], …], gzip-compressed,
reduced from the GeoNames list of cities above 15,000 inhabitants (CC BY 4.0, attribution shown on the Chaîne screen)
by `goulot dev fetch-cities`, run by the user from their Mac. Alternate names are kept when written in Latin or
Chinese, Japanese or Korean script (« Anvers », « Antwerpen », « 宁波 »), without airport codes. Without that file, a
site is placed by a point on the map.
"""

from __future__ import annotations

import gzip
import io
import json
import unicodedata
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from goulot.collect.base import fold

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "cities.json.gz"
SOURCE_URL = "https://download.geonames.org/export/dump/cities15000.zip"
ATTRIBUTION = "Villes : GeoNames (geonames.org), licence CC BY 4.0, réduites au nom, au pays et aux coordonnées."
MAX_ALTERNATES = 80
KEPT_SCRIPTS = ("LATIN", "CJK", "HIRAGANA", "KATAKANA", "HANGUL")
MAX_NAME = 60
MAX_UNZIPPED = 40 * 1024 * 1024  # the text file is about 8 MB: anything far larger is refused (zip bomb)
MAX_RESULTS = 12


@lru_cache(maxsize=1)
def _cities(path: Path = DATA_FILE) -> list[list[Any]] | None:
    if not path.is_file():
        return None
    data: list[list[Any]] = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    return data


def available() -> bool:
    return _cities() is not None


def _entry(row: list[Any]) -> dict[str, Any]:
    return {"name": row[0], "country_iso2": row[3], "lat": row[4], "lon": row[5], "population": row[6]}


def search(query: str, country_iso2: str | None = None, cities: list[list[Any]] | None = None
           ) -> list[dict[str, Any]]:
    """Cities whose name, ASCII name or an alternate name starts with the query; exact names first, then the most
    populated. Accents and case are ignored."""
    rows = cities if cities is not None else _cities()
    q = fold(query).strip()
    if not rows or len(q) < 2:
        return []
    found: list[tuple[int, int, list[Any]]] = []
    for row in rows:
        if country_iso2 and row[3] != country_iso2:
            continue
        names = [fold(row[0]), fold(row[1]), *(fold(a) for a in row[2])]
        if q in names:
            found.append((0, -int(row[6]), row))
        elif any(n.startswith(q) for n in names):
            found.append((1, -int(row[6]), row))
    found.sort(key=lambda t: (t[0], t[1]))
    return [_entry(r) for _, _, r in found[:MAX_RESULTS]]


def reduce_dump(zipped: bytes) -> list[list[Any]]:
    """Keep, from the GeoNames dump, the name, ASCII name, a few alternate names, the country, the coordinates and
    the population (for ordering). Columns: geonameid, name, asciiname, alternatenames, latitude, longitude, feature
    class, feature code, country code, … population (15)."""
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        info = archive.getinfo("cities15000.txt")
        if info.file_size > MAX_UNZIPPED:
            raise ValueError("fichier GeoNames anormalement gros")
        text = archive.read(info).decode("utf-8")
    out: list[list[Any]] = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 15 or len(cols[8]) != 2:
            continue
        name, ascii_name = cols[1][:MAX_NAME], cols[2][:MAX_NAME]
        seen = {fold(name), fold(ascii_name)}
        alternates: list[str] = []
        for alt in cols[3].split(","):
            alt = alt.strip()
            key = fold(alt)
            if not alt or len(alt) > MAX_NAME or key in seen or not _kept(alt):
                continue
            seen.add(key)
            alternates.append(alt)
            if len(alternates) >= MAX_ALTERNATES:
                break
        out.append([name, ascii_name, alternates, cols[8], round(float(cols[4]), 4), round(float(cols[5]), 4),
                    int(cols[14] or 0)])
    out.sort(key=lambda r: -r[6])
    return out


def _kept(name: str) -> bool:
    """A name people type: Latin or East Asian script, no digit, no link, not an airport code (« ANR »)."""
    if any(c.isdigit() for c in name) or "http" in name or (name.isupper() and len(name) <= 4):
        return False
    letters = [c for c in name if c.isalpha()]
    return bool(letters) and all(any(s in unicodedata.name(c, "") for s in KEPT_SCRIPTS) for c in letters)


def write(rows: list[list[Any]], path: Path = DATA_FILE) -> int:
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(gzip.compress(raw, mtime=0))  # same content, same bytes: no spurious change in git
    _cities.cache_clear()
    return path.stat().st_size
