"""Ma chaîne: the user's real supply chain, its checks and what it changes, computed on the Mac without AI
(docs/SPEC.md, section 11).

- Sites and links: a link says which part of the destination's purchases comes from the origin (for a link to the
  user, `me`, the part of the user's volume). The user's volume through a site is the sum, over its paths to the
  user, of the product of the shares (exposure).
- Upstream of a site, what the user did not declare flows like the typical chain: through the adopted node and its
  inputs when the site is a node of the typical chain, otherwise as the market does (world shares of each level).
  This assumption is shown on screen.
- X of an event on a site: lead times of the links from that site to the user's direct supplier, plus the stocks of
  the sites in between and of that supplier; the last link (to the user) is the lead time of the action window.

Nothing in this module logs, sends or stores anything: it computes from the decrypted document it is given.
"""

from __future__ import annotations

import csv
import io
import itertools
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from goulot.collect.base import fold

ME = "me"
SITE_ID = r"^[a-z0-9_-]{1,40}$"
NODE_KEY = r"^[a-z0-9_]{1,80}$"
WEIGHS_SHARE = 0.10  # a site carrying at least 10 % of the user's volume weighs for them
HIGH_SHARE = 0.50
LOGISTICS = {"port", "chokepoint", "waterway", "distributor"}
PRODUCING_ROLES = {"producer", "processor"}
EPS = 1e-6

ROLE_LABELS = {"producer": "Producteur", "processor": "Transformateur", "distributor": "Distributeur",
               "warehouse": "Entrepôt", "port": "Port"}
WATCH_LABELS = {"hazards": "aléas seulement", "city_material": "ville et matière",
                "city_material_company": "ville, matière et entreprise"}
ALT_LABELS = {"qualified": "qualifiée", "qualifying": "en cours de qualification", "not_qualified": "non qualifiée"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Alternative(_Strict):
    name: str = Field(min_length=1, max_length=120)
    status: Literal["qualified", "qualifying", "not_qualified"] = "not_qualified"
    switch_weeks: float | None = Field(default=None, ge=0, le=104)


class Site(_Strict):
    id: str = Field(pattern=SITE_ID)
    name: str = Field(min_length=1, max_length=120)
    role: Literal["producer", "processor", "distributor", "warehouse", "port"] = "producer"
    company: str = Field(default="", max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    city: str = Field(default="", max_length=120)
    country_iso2: str = Field(default="", pattern=r"^([A-Z]{2})?$")
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    node_key: str | None = Field(default=None, pattern=NODE_KEY)
    watch_level: Literal["hazards", "city_material", "city_material_company"] = "city_material"
    stock_days: float | None = Field(default=None, ge=0, le=730)
    alternatives: list[Alternative] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=1000)


class Link(_Strict):
    source: str = Field(pattern=SITE_ID)
    target: str = Field(pattern=SITE_ID)
    share_pct: float = Field(ge=0, le=100)
    transport_mode: Literal["sea", "river", "rail", "road", "pipeline", "air", "none", "unknown"] = "unknown"
    incoterm: str = Field(default="", max_length=12)
    lead_days: float | None = Field(default=None, ge=0, le=730)


class MyChain(_Strict):
    sites: list[Site] = Field(default_factory=list, max_length=200)
    links: list[Link] = Field(default_factory=list, max_length=400)
    not_used: list[str] = Field(default_factory=list, max_length=200)  # « Je ne passe pas par là »
    stock_at_user_days: float | None = Field(default=None, ge=0, le=730)
    stock_in_transit_days: float | None = Field(default=None, ge=0, le=730)
    notes: str = Field(default="", max_length=2000)


# --- checks -----------------------------------------------------------------------------------------------------
@dataclass
class Check:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _num(value: float) -> str:
    return f"{value:.1f}".replace(".", ",").replace(",0", "")


def check(doc: MyChain, typical: dict[str, Any] | None = None) -> Check:
    """Shares between 0 and 100 %, incoming shares of each site at most 100 %, a path to the user, positive lead
    times. A gap with the typical chain is reported, never corrected."""
    result = Check()
    ids = [s.id for s in doc.sites]
    names = {s.id: s.name for s in doc.sites}
    if ME in ids:
        result.errors.append("« me » est réservé : il désigne toi, au bout de la chaîne.")
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        result.errors.append(f"Sites en double : {', '.join(dupes)}.")
    known = set(ids)
    nodes = {n["node_key"]: n for n in (typical or {}).get("nodes", [])}
    incoming: dict[str, float] = {}
    for link in doc.links:
        if link.source not in known:
            result.errors.append(f"Lien depuis un site inconnu : {link.source}.")
            continue
        if link.target != ME and link.target not in known:
            result.errors.append(f"Lien vers un site inconnu : {link.target}.")
            continue
        if link.source == link.target:
            result.errors.append(f"{names[link.source]} ne peut pas s'approvisionner chez lui-même.")
        incoming[link.target] = incoming.get(link.target, 0.0) + link.share_pct
        if link.lead_days is None:
            result.warnings.append(f"Délai inconnu de {names[link.source]} vers {_target_name(link.target, names)} :"
                                   " il compte pour 0 dans X.")
    for target, total in incoming.items():
        if total > 100 + EPS:
            result.errors.append(f"Les parts qui arrivent chez {_target_name(target, names)} dépassent 100 % "
                                 f"({_num(total)} %).")
    g = graph(doc)
    if not nx.is_directed_acyclic_graph(g):
        result.errors.append("La chaîne tourne en rond : un site s'approvisionne, de proche en proche, chez lui-même.")
    for s in doc.sites:
        if ME not in g or not nx.has_path(g, s.id, ME):
            result.errors.append(f"{s.name} n'a aucun chemin jusqu'à toi.")
    for s in doc.sites:
        if s.node_key is not None and s.node_key not in nodes:
            result.errors.append(f"{s.name} est rattaché à un maillon inconnu de la chaîne type.")
            continue
        node = nodes.get(s.node_key or "")
        if node and s.country_iso2 and node["country_iso2"] not in ("unknown", s.country_iso2):
            result.warnings.append(f"{s.name} est en {s.country_iso2}, le maillon « {node['name']} » de la chaîne "
                                   f"type en {node['country_iso2']} : écart signalé, rien n'est corrigé.")
        if s.lat is None and s.watch_level != "hazards" and not s.city:
            result.warnings.append(f"{s.name} n'a ni ville ni point sur la carte : seuls son nom et son entreprise "
                                   "peuvent le relier à un événement.")
        elif s.lat is None:
            result.warnings.append(f"{s.name} n'a pas de point sur la carte : les aléas naturels proches ne "
                                   "peuvent pas le toucher.")
    for key in doc.not_used:
        if key not in nodes:
            result.errors.append("Un maillon écarté n'existe pas dans la chaîne type.")
    if typical and not result.errors:
        delays = typical.get("delays", {})
        for link in doc.links:
            site = next(s for s in doc.sites if s.id == link.source)
            ref = delays.get(site.node_key or "")
            if (link.target == ME and link.lead_days and ref and ref.get("days")
                    and abs(link.lead_days - ref["days"]) > 0.5 * ref["days"]):
                result.warnings.append(f"Délai de {site.name} jusqu'à toi : {_num(link.lead_days)} j, la chaîne "
                                       f"type estime {_num(ref['days'])} j. Écart signalé, rien n'est corrigé.")
    return result


def _target_name(target: str, names: dict[str, str]) -> str:
    return "toi" if target == ME else names.get(target, target)


def graph(doc: MyChain) -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node(ME)
    g.add_nodes_from(s.id for s in doc.sites)
    for link in doc.links:
        g.add_edge(link.source, link.target, share=link.share_pct / 100, lead=link.lead_days or 0.0,
                   lead_raw=link.lead_days)
    return g


# --- exposure ---------------------------------------------------------------------------------------------------
class Model:
    """A checked Ma chaîne together with the typical chain it overlays. Built once per computation."""

    def __init__(self, doc: MyChain, typical: dict[str, Any]) -> None:
        self.doc = doc
        self.sites = {s.id: s for s in doc.sites}
        self.g = graph(doc)
        self.nodes = {n["node_key"]: n for n in typical.get("nodes", [])}
        self.edges = [e for e in typical.get("edges", []) if e["from_key"] in self.nodes and e["to_key"] in self.nodes]
        self.delays: dict[str, dict[str, float]] = typical.get("delays", {})
        self.not_used = set(doc.not_used)
        sinks = [k for k, n in self.nodes.items() if n["tier"] == 0]
        self.sink = sinks[0] if len(sinks) == 1 else None
        self.exposure = self._exposure()
        self.weights = self._edge_weights()

    # user's volume through each site
    def _exposure(self) -> dict[str, float]:
        out: dict[str, float] = {ME: 1.0}
        if not nx.is_directed_acyclic_graph(self.g):
            return out
        for node in reversed(list(nx.topological_sort(self.g))):
            if node == ME:
                continue
            out[node] = sum(d["share"] * out.get(t, 0.0) for _, t, d in self.g.out_edges(node, data=True))
        return out

    def declared_in(self, site_id: str) -> float:
        return float(sum(d["share"] for _, _, d in self.g.in_edges(site_id, data=True)))

    def unexplained(self, site_id: str) -> float:
        """Part of the site's own supply that the user did not declare: it flows like the typical chain."""
        return max(0.0, 1.0 - self.declared_in(site_id))

    def link_flows(self) -> list[dict[str, Any]]:
        """Share of the user's volume through each link (shown as the thickness of the link)."""
        return [{"source": a, "target": b, "flow_pct": round(100 * d["share"] * self.exposure.get(b, 0.0), 2)}
                for a, b, d in self.g.edges(data=True)]

    def direct_suppliers(self) -> list[str]:
        return [a for a, _ in self.g.in_edges(ME)]

    def weighs(self, site_id: str) -> bool:
        """At least 10 % of the user's volume, or their sole supplier."""
        if self.exposure.get(site_id, 0.0) >= WEIGHS_SHARE - EPS:
            return True
        suppliers = self.direct_suppliers()
        return suppliers == [site_id]

    # typical chain: fraction of a node's supply coming from each upstream node
    def _edge_weights(self) -> dict[tuple[str, str], float]:
        weights: dict[tuple[str, str], float] = {}
        by_target: dict[str, list[dict[str, Any]]] = {}
        for e in self.edges:
            if e["from_key"] in self.not_used or e["to_key"] in self.not_used:
                continue
            by_target.setdefault(e["to_key"], []).append(e)
        for target, edges in by_target.items():
            # Edge share when known; else the world share of the origin; else an equal part of what is left.
            others = []
            for e in edges:
                world = self.nodes[e["from_key"]].get("global_share_pct")
                if e.get("share_pct") is not None:
                    weights[(e["from_key"], target)] = float(e["share_pct"]) / 100
                elif world is not None:
                    weights[(e["from_key"], target)] = float(world) / 100
                else:
                    others.append(e)
            rest = max(0.0, 1.0 - sum(w for (_, b), w in weights.items() if b == target))
            for e in others:
                weights[(e["from_key"], target)] = rest / len(others)
        return weights

    def _through(self, touched: frozenset[str]) -> dict[str, float]:
        """node -> fraction of its supply that passes through one of `touched` (itself included)."""
        weights = self.weights
        preds: dict[str, list[tuple[str, float]]] = {}
        for (a, b), w in weights.items():
            preds.setdefault(b, []).append((a, w))
        memo: dict[str, float] = {}

        def p(node: str, stack: frozenset[str]) -> float:
            if node in self.not_used:
                return 0.0
            if node in touched:
                return 1.0
            if node in memo:
                return memo[node]
            if node in stack:
                return 0.0  # a cycle in the typical chain carries nothing more
            value = min(1.0, sum(w * p(a, stack | {node}) for a, w in preds.get(node, [])))
            memo[node] = value
            return value

        return {k: p(k, frozenset()) for k in self.nodes}

    def producers(self) -> list[str]:
        """Typical-chain nodes that make the purchased material: tier 1, neither a material nor logistics."""
        return [k for k, n in self.nodes.items() if n["tier"] == 1 and n["node_type"] not in LOGISTICS | {"material"}]

    def _start(self, site: Site) -> tuple[dict[str, float], set[str]]:
        """Where the undeclared supply of a site enters the typical chain: start nodes with their weight, and the
        nodes not to count themselves (the market's producers are not the user's unlinked producer)."""
        if site.node_key and site.node_key in self.nodes:
            return {site.node_key: 1.0}, set()
        if self.sink is None:
            return {}, set()
        if site.role in PRODUCING_ROLES:
            producers = [k for k in self.producers() if k not in self.not_used]
            shares = {k: float(self.nodes[k].get("global_share_pct") or 0.0) for k in producers}
            total = sum(shares.values())
            if total <= 0:
                return {k: 1 / len(producers) for k in producers}, set(producers) if producers else set()
            return {k: v / total for k, v in shares.items()}, set(producers)
        return {self.sink: 1.0}, {self.sink}

    def chain_fraction(self, site: Site, touched_nodes: frozenset[str]) -> float:
        """Fraction of the site's undeclared supply that passes through the touched typical-chain nodes."""
        if not touched_nodes:
            return 0.0
        start, excluded = self._start(site)
        through = self._through(touched_nodes - excluded)
        weights = self.weights
        total = 0.0
        for node, w in start.items():
            if node in excluded:
                upstream = sum(wt * through.get(a, 0.0) for (a, b), wt in weights.items() if b == node)
                total += w * min(1.0, upstream)
            else:
                total += w * through.get(node, 0.0)
        return min(1.0, total)

    def event_exposure(self, sites: Iterable[str], nodes: Iterable[str]) -> float:
        """Share of the user's volume that passes through a touched site of Ma chaîne or a touched node of the
        typical chain upstream of their sites (0 when the event is not on their paths)."""
        touched_sites = {s for s in sites if s in self.sites}
        touched_sites |= {s.id for s in self.doc.sites if s.node_key and s.node_key in set(nodes)}
        touched_nodes = frozenset(n for n in nodes if n in self.nodes)
        h: dict[str, float] = {}
        if not nx.is_directed_acyclic_graph(self.g):
            return 0.0
        for node in nx.topological_sort(self.g):
            if node == ME:
                continue
            if node in touched_sites:
                h[node] = 1.0
                continue
            declared = sum(d["share"] * h.get(a, 0.0) for a, _, d in self.g.in_edges(node, data=True))
            site = self.sites[node]
            h[node] = min(1.0, declared + self.unexplained(node) * self.chain_fraction(site, touched_nodes))
        return min(1.0, float(sum(d["share"] * h.get(a, 0.0) for a, _, d in self.g.in_edges(ME, data=True))))

    # delays
    def private_delay(self, site_id: str, lead_override: dict[str, float] | None = None) -> dict[str, Any] | None:
        """From a site to the user's direct supplier: {"days", "buffers", "supplier", "lead_days", "path"}.
        The direct supplier itself gives 0 (docs/SPEC.md, section 5, supplier signals)."""
        lead_override = lead_override or {}
        best: dict[str, Any] | None = None
        for path in nx.all_simple_paths(self.g, site_id, ME) if site_id in self.g else []:
            days = buffers = 0.0
            for a, b in itertools.pairwise(path[:-1]):  # every link but the last one, to the user
                lead = lead_override.get(a, self.g[a][b]["lead"])
                stock = self.sites[b].stock_days or 0.0
                days += lead + stock
                buffers += stock
            supplier = path[-2]
            final = self.g[supplier][ME]
            lead_days = lead_override.get(supplier, final["lead_raw"])
            if best is None or days < best["days"]:
                best = {"days": days, "buffers": buffers, "supplier": supplier, "lead_days": lead_days, "path": path}
        return best

    def node_delay(self, node_key: str, site: Site) -> dict[str, float] | None:
        """Typical-chain delay from a node to where it feeds the site (low, central, high bounds)."""
        ref = self.delays.get(node_key)
        if ref is None:
            return None
        if site.node_key and site.node_key in self.delays:
            base = self.delays[site.node_key]
        elif site.role in PRODUCING_ROLES and self.producers():
            known = [self.delays[k] for k in self.producers() if k in self.delays]
            base = min(known, key=lambda d: d["days"]) if known else {"days": 0.0, "low": 0.0, "high": 0.0}
        else:
            base = {"days": 0.0, "low": 0.0, "high": 0.0, "buffers": 0.0}
        return {k: max(0.0, ref.get(k, 0.0) - base.get(k, 0.0)) for k in ("days", "low", "high", "buffers")}

    def plan_b(self, site_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Qualified alternatives of the touched sites and of the sites downstream of them."""
        wanted: set[str] = set()
        for sid in site_ids:
            if sid in self.g:
                wanted |= {sid} | (nx.descendants(self.g, sid) - {ME})
        out = []
        for site in self.doc.sites:
            if site.id in wanted:
                out += [{"site": site.name, "name": a.name, "status": ALT_LABELS[a.status],
                         "switch_weeks": a.switch_weeks} for a in site.alternatives if a.status != "not_qualified"]
        return out


# --- hidden concentration --------------------------------------------------------------------------------------
def _hhi(by_country: dict[str, float]) -> dict[str, Any]:
    if not by_country:
        return {"top_country": None, "top_share_pct": None, "hhi": None}
    top = max(by_country, key=lambda c: by_country[c])
    return {"top_country": top, "top_share_pct": round(by_country[top], 1),
            "hhi": round(sum(v * v for v in by_country.values()))}


def market_concentration(model: Model) -> dict[str, Any]:
    by_country: dict[str, float] = {}
    for k in model.producers():
        n = model.nodes[k]
        if n.get("global_share_pct") is not None and n["country_iso2"] != "unknown":
            by_country[n["country_iso2"]] = by_country.get(n["country_iso2"], 0.0) + float(n["global_share_pct"])
    return _hhi(by_country)


def my_concentration(model: Model) -> dict[str, Any]:
    """Shares by country at the production level, on the user's declared volume: a producing site counts with its
    country; what a distributor does not declare is spread like the market."""
    market: dict[str, float] = {}
    for k in model.producers():
        n = model.nodes[k]
        if n.get("global_share_pct") is not None and n["country_iso2"] != "unknown":
            market[n["country_iso2"]] = market.get(n["country_iso2"], 0.0) + float(n["global_share_pct"])
    market_total = sum(market.values())
    by_country: dict[str, float] = {}

    def add(country: str, weight: float) -> None:
        if weight > 0:
            by_country[country] = by_country.get(country, 0.0) + weight

    def walk(node: str, weight: float, depth: int) -> None:
        if depth > 50:
            return
        for a, _, d in model.g.in_edges(node, data=True):
            site = model.sites[a]
            w = weight * d["share"]
            node_country = (model.nodes.get(site.node_key or "") or {}).get("country_iso2", "unknown")
            country = site.country_iso2 or (node_country if node_country != "unknown" else "")
            if site.role in PRODUCING_ROLES:
                add(country or "??", w)
            else:
                walk(a, w, depth + 1)
                rest = w * model.unexplained(a)
                for c, v in market.items():
                    add(c, rest * v / market_total if market_total else 0.0)

    walk(ME, 1.0, 0)
    total = sum(by_country.values())
    if total <= 0:
        return _hhi({})
    return _hhi({c: 100 * v / total for c, v in by_country.items()})


# --- CSV import (in memory) -------------------------------------------------------------------------------------
CSV_COLUMNS = ["site", "role", "entreprise", "ville", "pays", "latitude", "longitude", "fournit", "part_pct",
               "delai_jours", "transport", "incoterm", "stock_jours", "surveillance", "maillon_chaine_type"]
CSV_TEMPLATE = (
    ";".join(CSV_COLUMNS) + "\n"
    "Distributeur Exemple;distributeur;Exemple SA;Anvers;BE;;;moi;60;7;route;DAP;21;ville et matière;\n"
    "Usine Exemple;producteur;Exemple Chemicals;Ningbo;CN;;;Distributeur Exemple;100;35;mer;FOB;;"
    "ville et matière;\n"
)
_ROLES_FR = {"producteur": "producer", "transformateur": "processor", "distributeur": "distributor",
             "entrepot": "warehouse", "port": "port"}
_MODES_FR = {"mer": "sea", "fleuve": "river", "rail": "rail", "route": "road", "pipeline": "pipeline",
             "avion": "air", "aucun": "none", "": "unknown"}
_WATCH_FR = {"aleas seulement": "hazards", "ville et matiere": "city_material",
             "ville, matiere et entreprise": "city_material_company", "": "city_material"}
MAX_CSV_ROWS = 300


def _site_id(name: str, taken: set[str]) -> str:
    base = "".join(c if c.isalnum() else "-" for c in fold(name))[:30].strip("-") or "site"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    taken.add(candidate)
    return candidate


def parse_csv(text: str) -> tuple[MyChain | None, list[str]]:
    """The template's columns, `;` or `,` separated, read in memory only. Returns the document or the errors,
    row by row (line numbers as in a spreadsheet)."""
    errors: list[str] = []
    sample = text[:2000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=delimiter)
    header = [fold(h or "").strip() for h in (reader.fieldnames or [])]
    missing = [c for c in ("site", "fournit", "part_pct") if c not in header]
    if missing:
        return None, [f"Colonnes absentes : {', '.join(missing)} (voir le modèle)."]
    rows = []
    for i, raw in enumerate(reader, start=2):
        if i - 1 > MAX_CSV_ROWS:
            errors.append(f"Plus de {MAX_CSV_ROWS} lignes : le reste est ignoré.")
            break
        rows.append((i, {fold(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}))
    taken: set[str] = set()
    ids = {}
    sites: list[dict[str, Any]] = []
    for i, r in rows:
        name = r.get("site", "")
        if not name:
            errors.append(f"Ligne {i} : nom du site vide.")
            continue
        if fold(name) in ids:
            continue  # a site that supplies several destinations: one line per link
        ids[fold(name)] = _site_id(name, taken)
        role = _ROLES_FR.get(fold(r.get("role", "")) or "producteur")
        if role is None:
            errors.append(f"Ligne {i} : rôle inconnu « {r.get('role')} ».")
            role = "producer"
        sites.append({
            "id": ids[fold(name)], "name": name[:120], "role": role, "company": r.get("entreprise", "")[:120],
            "city": r.get("ville", "")[:120], "country_iso2": r.get("pays", "").upper()[:2],
            "lat": _float(r.get("latitude")), "lon": _float(r.get("longitude")),
            "node_key": r.get("maillon_chaine_type") or None, "stock_days": _float(r.get("stock_jours")),
            "watch_level": _WATCH_FR.get(fold(r.get("surveillance", "")), "city_material"),
        })
    links = []
    for i, r in rows:
        target_name = r.get("fournit", "")
        target = ME if fold(target_name) in ("moi", "me", "toi") else ids.get(fold(target_name))
        if fold(r.get("site", "")) not in ids:
            continue
        if target is None:
            errors.append(f"Ligne {i} : « fournit » doit être « moi » ou le nom d'un autre site du fichier.")
            continue
        share = _float(r.get("part_pct"))
        if share is None:
            errors.append(f"Ligne {i} : part en % manquante ou illisible.")
            continue
        links.append({"source": ids[fold(r["site"])], "target": target, "share_pct": share,
                      "lead_days": _float(r.get("delai_jours")), "incoterm": r.get("incoterm", "")[:12],
                      "transport_mode": _MODES_FR.get(fold(r.get("transport", "")), "unknown")})
    if errors:
        return None, errors
    try:
        return MyChain.model_validate({"sites": sites, "links": links}), []
    except ValidationError as exc:
        return None, [f"Valeur hors bornes : {'.'.join(str(p) for p in e['loc'])}." for e in exc.errors()[:20]]


def _float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value.replace(" ", "").replace(",", "."))
    except ValueError:
        return None

