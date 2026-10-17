"""Model outputs: JSON Schema first, then business checks (same rules as `schemas/check_schemas.py`).

`SchemaError` means the output is rejected (the call is retried once). Business anomalies do not reject the
chain: they are returned per node or edge, which then drops to low confidence and shows on the Chaîne screen.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from goulot.llm.prompts import load_schema_text

ISO2 = re.compile(r"^[A-Z]{2}$")
DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")  # a year alone is a valid, if coarse, due date
NODE_ID = re.compile(r"^[a-z0-9_]+$")
ENUM_FIELDS = {
    "node_type", "confidence", "relation", "transport_mode", "risk_type",
}


class SchemaError(ValueError):
    pass


# Allowed values checked by code: since supply_chain_v2 the lists are in the descriptions, not in `enum`
# (an `enum` per field made the compiled grammar too large for the API, 2026-10-16).
CHAIN_VALUES: dict[str, tuple[tuple[str, ...], str | None]] = {
    "node_type": (("material", "mine", "plant", "recycler", "country", "company", "port", "chokepoint", "waterway",
                   "distributor", "other"), "other"),
    "confidence": (("high", "medium", "low"), "low"),
    "relation": (("feedstock_of", "produces", "ships_via", "yields_coproduct", "supplies"), None),
    "transport_mode": (("sea", "river", "rail", "road", "pipeline", "air", "none", "unknown"), "unknown"),
    "risk_type": (("geographic_concentration", "single_supplier", "export_control", "logistics_chokepoint",
                   "seasonality", "recurring_maintenance", "regulatory", "coproduct_dependency", "other"), "other"),
}


@lru_cache(maxsize=8)
def schema(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(load_schema_text(name))
    return data


def _lower_enums(node: Any) -> Any:
    """Enum casing is not guaranteed by the API: normalize before validation (CLAUDE.md, AI calls)."""
    if isinstance(node, dict):
        return {k: (v.lower() if k in ENUM_FIELDS and isinstance(v, str) else _lower_enums(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_lower_enums(v) for v in node]
    return node


def parse_json_output(content: list[dict[str, Any]]) -> Any:
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError("output is not JSON") from exc


def validate_schema(name: str, data: Any) -> dict[str, Any]:
    data = _lower_enums(data)
    errors = sorted(Draft202012Validator(schema(name)).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        raise SchemaError(f"{len(errors)} schema error(s), first at {list(first.path)}: {first.validator}")
    if not isinstance(data, dict):
        raise SchemaError("output is not an object")
    return data


@dataclass
class ChainCheck:
    fatal: list[str] = field(default_factory=list)  # the graph cannot be used as is
    nodes: dict[str, list[str]] = field(default_factory=dict)  # node_id -> anomalies (French)
    edges: dict[int, list[str]] = field(default_factory=dict)  # edge index -> anomalies (French)
    general: list[str] = field(default_factory=list)

    def node(self, nid: str, msg: str) -> None:
        self.nodes.setdefault(nid, []).append(msg)

    def edge(self, idx: int, msg: str) -> None:
        self.edges.setdefault(idx, []).append(msg)


def normalize_chain_values(chain: dict[str, Any], out: ChainCheck) -> None:
    """Values outside the allowed lists are replaced by a safe default (or the edge flagged), tiers kept in 0-3."""

    def fix(item: dict[str, Any], key: str) -> str | None:
        allowed, default = CHAIN_VALUES[key]
        value = str(item[key]).strip().lower()
        if value in allowed:
            item[key] = value
            return None
        item[key] = default if default is not None else allowed[-1]
        return f"valeur inattendue pour {key} : « {value[:40]} »"

    for n in chain["nodes"]:
        for key in ("node_type", "confidence"):
            msg = fix(n, key)
            if msg:
                out.node(n["node_id"], msg)
        if not isinstance(n["tier"], int) or not 0 <= n["tier"] <= 3:
            out.node(n["node_id"], "niveau hors de 0 à 3")
            n["tier"] = min(max(int(n["tier"]), 1), 3)
    for i, e in enumerate(chain["edges"]):
        for key in ("relation", "transport_mode", "confidence"):
            msg = fix(e, key)
            if msg:
                out.edge(i, msg)
    for r in chain["structural_risks"]:
        if fix(r, "risk_type"):
            out.general.append("type de risque inattendu remplacé par « other »")


def check_chain(chain: dict[str, Any], allowed_sources: set[str], expected_cas: str) -> ChainCheck:
    """Business checks on a schema-valid supply chain. Only out-of-list values are normalized in place."""
    out = ChainCheck()
    normalize_chain_values(chain, out)
    nodes = {n["node_id"]: n for n in chain["nodes"]}
    if len(nodes) != len(chain["nodes"]):
        out.fatal.append("identifiants de maillons en double")
    if chain["material"]["cas"].strip() != expected_cas:
        out.fatal.append("le numéro CAS renvoyé diffère du CAS demandé")
    tier0 = [nid for nid, n in nodes.items() if n["tier"] == 0]
    if len(tier0) != 1 or nodes[tier0[0]]["node_type"] != "material":
        out.fatal.append("il faut exactement un maillon de niveau 0, la matière achetée")
    out_edges: dict[str, list[str]] = {nid: [] for nid in nodes}
    for i, e in enumerate(chain["edges"]):
        if e["from_id"] not in nodes or e["to_id"] not in nodes:
            out.edge(i, "lien vers un maillon inconnu")
            continue
        if e["from_id"] == e["to_id"]:
            out.edge(i, "lien d'un maillon vers lui-même")
            continue
        out_edges[e["from_id"]].append(e["to_id"])
        if e["share_pct"] is not None and not 0 <= e["share_pct"] <= 100:
            out.edge(i, "part hors de 0 à 100 %")
        if e["lead_time_days"] is not None and e["lead_time_days"] < 0:
            out.edge(i, "délai négatif")
        if e["confidence"] in ("high", "medium") and not e["source_ids"]:
            out.edge(i, "confiance haute ou moyenne sans source")
        unknown = set(e["source_ids"]) - allowed_sources
        if unknown:
            out.edge(i, "source inconnue citée")
    if not out.fatal:
        sink = tier0[0]
        if out_edges[sink]:
            out.node(sink, "la matière achetée ne doit avoir aucun lien sortant")
        for nid in nodes:
            if nid != sink and not out_edges[nid]:
                out.node(nid, "maillon sans lien vers l'aval")
            elif nid != sink and not _reaches(nid, sink, out_edges):
                out.node(nid, "aucun chemin jusqu'à la matière achetée")
        if _has_cycle(out_edges):
            out.general.append("le graphe contient un cycle : chaîne à revoir")
    for n in chain["nodes"]:
        nid = n["node_id"]
        if not NODE_ID.match(nid):
            out.node(nid, "identifiant mal formé")
        if (n["lat"] is None) != (n["lon"] is None):
            out.node(nid, "latitude et longitude incomplètes")
        elif n["lat"] is not None and not (-90 <= n["lat"] <= 90 and -180 <= n["lon"] <= 180):
            out.node(nid, "coordonnées hors limites")
        if n["node_type"] in ("country", "waterway") and n["lat"] is not None:
            out.node(nid, "coordonnées pour un pays entier ou un fleuve")
        if n["global_share_pct"] is not None and not 0 <= n["global_share_pct"] <= 100:
            out.node(nid, "part mondiale hors de 0 à 100 %")
        if n["country_iso2"] != "unknown" and not ISO2.match(n["country_iso2"]):
            out.node(nid, "code pays mal formé")
        if n["confidence"] in ("high", "medium") and not n["source_ids"]:
            out.node(nid, "confiance haute ou moyenne sans source")
        if set(n["source_ids"]) - allowed_sources:
            out.node(nid, "source inconnue citée")
        if n["buffer_days"] is not None and n["buffer_days"] < 0:
            out.node(nid, "stock tampon négatif")
    for part in ("routes", "substitutes", "structural_risks", "upcoming_dates"):
        for item in chain[part]:
            if set(item["source_ids"]) - allowed_sources:
                out.general.append(f"{part} : source inconnue citée")
            if set(item.get("node_ids", [])) - set(nodes):
                out.general.append(f"{part} : maillon inconnu cité")
    for d in chain["upcoming_dates"]:
        if not DATE.match(d["date"]):
            out.general.append(f"échéance mal datée : {d['date'][:20]}")
    for r in chain["routes"]:
        if r["global_share_pct"] is not None and not 0 <= r["global_share_pct"] <= 100:
            out.general.append("part d'une voie hors de 0 à 100 %")
    return out


def _reaches(start: str, target: str, out_edges: dict[str, list[str]]) -> bool:
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur not in seen:
            seen.add(cur)
            stack.extend(out_edges.get(cur, []))
    return False


def _has_cycle(out_edges: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}
    for root in out_edges:
        if root in state:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            nid, i = stack[-1]
            nexts = out_edges.get(nid, [])
            if i < len(nexts):
                stack[-1] = (nid, i + 1)
                nxt = nexts[i]
                if state.get(nxt) == 1:
                    return True
                if nxt not in state:
                    state[nxt] = 1
                    stack.append((nxt, 0))
            else:
                state[nid] = 2
                stack.pop()
    return False
