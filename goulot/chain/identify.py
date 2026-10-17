"""Step 1: identify a substance from a name or a CAS number (docs/SPEC.md, section 3).

PubChem (PUG REST) gives the CAS, the name, the formula and the synonyms; Wikidata (property P231) adds the
French label and confirms the CAS. Customs codes: ECICS has no API and its full download is not wired yet, so
the HS codes proposed by the structuring call are kept and marked « à vérifier » (fallback of section 3).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx2

from goulot.net.client import fetch

log = logging.getLogger("goulot.chain.identify")

CAS_RE = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
WIKIDATA = "https://query.wikidata.org/sparql"
MAX_SYNONYMS = 12
MAX_QUERY = 100


class IdentificationError(ValueError):
    """Message in French, shown on the « Ajouter une matière » screen."""


@dataclass
class Identity:
    cas: str
    name: str
    formula: str = ""
    synonyms: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)  # {name, url}: built by the code only
    name_fr: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "cas": self.cas, "name": self.name, "name_fr": self.name_fr, "formula": self.formula,
            "synonyms": self.synonyms, "sources": self.sources,
        }


def normalize_cas(text: str) -> str | None:
    """A valid CAS number (check digit verified), or None."""
    m = CAS_RE.match(text.strip())
    if not m:
        return None
    digits = (m.group(1) + m.group(2))[::-1]
    if sum((i + 1) * int(d) for i, d in enumerate(digits)) % 10 != int(m.group(3)):
        return None
    return f"{int(m.group(1))}-{m.group(2)}-{m.group(3)}"


def _json(client: httpx2.Client, url: str, *, params: dict[str, str] | None = None) -> Any:
    result = fetch(client, url, params=params, max_bytes=2 * 1024 * 1024)
    if result.status == 404:
        return None
    if result.status != 200:
        raise IdentificationError(f"Source d'identification indisponible (HTTP {result.status}) ; réessayez.")
    try:
        return json.loads(result.content)
    except json.JSONDecodeError:
        raise IdentificationError("Réponse illisible de la source d'identification.") from None


def _pubchem(client: httpx2.Client, query: str) -> tuple[int, dict[str, Any], list[str]] | None:
    name = quote(query, safe="")
    data = _json(client, f"{PUBCHEM}/compound/name/{name}/property/MolecularFormula,IUPACName,Title/JSON")
    if not data:
        return None
    props = (data.get("PropertyTable") or {}).get("Properties") or []
    if not props:
        return None
    first = props[0]
    cid = int(first["CID"])
    syn = _json(client, f"{PUBCHEM}/compound/cid/{cid}/synonyms/JSON") or {}
    info = (syn.get("InformationList") or {}).get("Information") or [{}]
    synonyms = [str(s) for s in info[0].get("Synonym") or []]
    return cid, first, synonyms


def _wikidata_label(client: httpx2.Client, cas: str) -> tuple[str, str] | None:
    """(QID, French or English label) for the item whose P231 is `cas`. `cas` is validated before use."""
    if normalize_cas(cas) != cas:
        return None
    sparql = (
        'SELECT ?item ?itemLabel WHERE { ?item wdt:P231 "' + cas + '" . '
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "fr,en". } } LIMIT 1'
    )
    try:
        data = _json(client, WIKIDATA, params={"query": sparql, "format": "json"})
    except IdentificationError:
        return None  # Wikidata is a complement: PubChem alone is enough
    rows = ((data or {}).get("results") or {}).get("bindings") or []
    if not rows:
        return None
    qid = rows[0]["item"]["value"].rsplit("/", 1)[-1]
    return qid, rows[0].get("itemLabel", {}).get("value", "")


def identify(client: httpx2.Client, query: str) -> Identity:
    query = query.strip()
    if not query or len(query) > MAX_QUERY:
        raise IdentificationError("Saisissez un nom ou un numéro CAS (100 caractères au plus).")
    looks_like_cas = bool(re.match(r"^\d+-\d+-\d+$", query))
    cas = normalize_cas(query)
    if looks_like_cas and cas is None:
        raise IdentificationError("Ce numéro CAS n'est pas valide (chiffre de contrôle faux).")
    found = _pubchem(client, cas or query)
    if found is None:
        raise IdentificationError("Substance introuvable dans PubChem. Vérifiez l'orthographe ou le numéro CAS.")
    cid, props, synonyms = found
    cas_synonyms = [c for c in (normalize_cas(s) for s in synonyms) if c]
    if cas is None:
        if not cas_synonyms:
            raise IdentificationError("PubChem ne donne pas de numéro CAS pour cette substance : saisissez-le.")
        cas = cas_synonyms[0]
    elif cas_synonyms and cas not in cas_synonyms:
        log.info("CAS not among PubChem synonyms (cid=%s)", cid)
    name = str(props.get("Title") or props.get("IUPACName") or query)
    readable = [s for s in synonyms if not normalize_cas(s) and len(s) <= 60]
    identity = Identity(
        cas=cas,
        name=name,
        formula=str(props.get("MolecularFormula") or ""),
        synonyms=readable[:MAX_SYNONYMS],
        sources=[{"name": "PubChem", "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"}],
    )
    wd = _wikidata_label(client, cas)
    if wd:
        qid, label = wd
        identity.name_fr = label
        identity.sources.append({"name": "Wikidata", "url": f"https://www.wikidata.org/wiki/{qid}"})
    return identity
