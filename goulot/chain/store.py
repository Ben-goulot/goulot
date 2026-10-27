"""Typical chain in SQLite (public data, not encrypted: docs/SPEC.md, section 2).

Bitemporal rows: a correction closes the current row (`valid_to`) and inserts a new one. A reconstruction may
replace a chain only before validation.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from goulot.db.connection import utcnow

NODE_FIELDS = (
    "node_type", "name", "product", "tier", "country_iso2", "place", "lat", "lon", "global_share_pct",
    "buffer_days", "capacity_text", "confidence", "notes",
)
EDGE_FIELDS = ("relation", "transport_mode", "lead_time_days", "share_pct", "confidence")
EDITABLE_NODE_FIELDS = (
    "name", "product", "country_iso2", "place", "lat", "lon", "global_share_pct", "buffer_days", "capacity_text",
    "confidence", "notes",
)
EDITABLE_EDGE_FIELDS = ("transport_mode", "lead_time_days", "share_pct", "confidence")


class ChainStateError(ValueError):
    pass


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def official_hs_codes(cn_codes: list[str]) -> list[dict[str, Any]]:
    """HS codes (6 digits) from the ECICS CN codes, verified; the CN code is kept for Eurostat Comext."""
    out: list[dict[str, Any]] = []
    for cn8 in cn_codes:
        if isinstance(cn8, str) and cn8.isdigit() and len(cn8) == 8 and all(h["code"] != cn8[:6] for h in out):
            out.append({"code": cn8[:6], "cn8": cn8, "origin": "ecics", "verified": True})
    return out


def create_material(conn: sqlite3.Connection, identity: dict[str, Any]) -> str:
    row = conn.execute("SELECT id FROM materials WHERE cas = ?", (identity["cas"],)).fetchone()
    if row:
        return str(row["id"])
    material_id = str(uuid.uuid4())
    now = utcnow()
    conn.execute(
        "INSERT INTO materials (id, name, cas, formula, synonyms, hs_codes, identity_sources, status, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
        (material_id, identity.get("name_fr") or identity["name"], identity["cas"], identity.get("formula", ""),
         _j(identity.get("synonyms", [])), _j(official_hs_codes(identity.get("cn_codes", []))),
         _j(identity.get("sources", [])), now, now),
    )
    return material_id


def get_material(conn: sqlite3.Connection, material_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM materials WHERE id = ?", (material_id,)).fetchone()
    if row is None:
        return None
    m = dict(row)
    for key in ("synonyms", "hs_codes", "identity_sources"):
        m[key] = json.loads(m[key])
    return m


def list_materials(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, cas, status, updated_at FROM materials WHERE status != 'archived' ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def set_status(conn: sqlite3.Connection, material_id: str, status: str) -> None:
    conn.execute("UPDATE materials SET status = ?, updated_at = ? WHERE id = ?", (status, utcnow(), material_id))


def clear_chain(conn: sqlite3.Connection, material_id: str) -> None:
    for sql in (
        "DELETE FROM node_sources WHERE node_id IN (SELECT id FROM chain_nodes WHERE material_id = ?)",
        "DELETE FROM edge_sources WHERE edge_id IN (SELECT id FROM chain_edges WHERE material_id = ?)",
        "DELETE FROM chain_nodes WHERE material_id = ?",
        "DELETE FROM chain_edges WHERE material_id = ?",
        "DELETE FROM chain_routes WHERE material_id = ?",
        "DELETE FROM chain_risks WHERE material_id = ?",
        "DELETE FROM chain_dates WHERE material_id = ?",
        "DELETE FROM chain_reports WHERE material_id = ?",
        "DELETE FROM sources WHERE material_id = ?",
    ):
        conn.execute(sql, (material_id,))


def save_chain(
    conn: sqlite3.Connection,
    material_id: str,
    chain: dict[str, Any],
    sources: list[dict[str, str]],
    report: str,
    *,
    node_anomalies: dict[str, list[str]],
    edge_anomalies: dict[int, list[str]],
    general: list[str],
) -> None:
    material = get_material(conn, material_id)
    if material is None:
        raise ChainStateError("matière inconnue")
    if material["status"] == "validated":
        raise ChainStateError("une chaîne validée ne se reconstruit pas : corrigez ses maillons")
    clear_chain(conn, material_id)
    now = utcnow()
    source_ids: dict[str, int] = {}
    for s in sources:
        cur = conn.execute(
            "INSERT INTO sources (material_id, ref, url, title, publisher, page_age, retrieved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (material_id, s["ref"], s["url"], s.get("title", ""), s.get("publisher", ""), s.get("page_age", ""), now),
        )
        source_ids[s["ref"]] = int(cur.lastrowid or 0)
    from goulot.chain.places import computed_position

    for n in chain["nodes"]:
        anomalies = [*n.get("anomalies", []), *node_anomalies.get(n["node_id"], [])]
        confidence = "low" if anomalies else n["confidence"]
        lat, lon, coord_source = n["lat"], n["lon"], "ai"
        if lat is None or lon is None:
            lat, lon, coord_source = computed_position(n)
        cur = conn.execute(
            "INSERT INTO chain_nodes (material_id, node_key, node_type, name, product, tier, country_iso2, place, lat, "
            "lon, global_share_pct, buffer_days, buffer_is_default, capacity_text, confidence, notes, anomalies, "
            "valid_from, recorded_at, coord_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (material_id, n["node_id"], n["node_type"], n["name"], n["product"], n["tier"], n["country_iso2"],
             n["place"], lat, lon, n["global_share_pct"], n["buffer_days"],
             int(bool(n.get("buffer_is_default"))), n["capacity_text"], confidence, n["notes"], _j(anomalies),
             now, now, coord_source),
        )
        for ref in n["source_ids"]:
            if ref in source_ids:
                conn.execute("INSERT OR IGNORE INTO node_sources VALUES (?, ?)", (cur.lastrowid, source_ids[ref]))
    for i, e in enumerate(chain["edges"]):
        anomalies = edge_anomalies.get(i, [])
        confidence = "low" if anomalies else e["confidence"]
        cur = conn.execute(
            "INSERT INTO chain_edges (material_id, from_key, to_key, relation, transport_mode, lead_time_days, "
            "lead_time_basis, share_pct, confidence, anomalies, valid_from, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (material_id, e["from_id"], e["to_id"], e["relation"], e["transport_mode"], e["lead_time_days"],
             e.get("lead_time_basis", "source"), e["share_pct"], confidence, _j(anomalies), now, now),
        )
        for ref in e["source_ids"]:
            if ref in source_ids:
                conn.execute("INSERT OR IGNORE INTO edge_sources VALUES (?, ?)", (cur.lastrowid, source_ids[ref]))
    for r in chain["routes"]:
        conn.execute(
            "INSERT INTO chain_routes (material_id, route_key, name, global_share_pct, description, node_keys, "
            "source_refs, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (material_id, r["route_id"], r["name"], r["global_share_pct"], r["description"], _j(r["node_ids"]),
             _j(r["source_ids"]), now),
        )
    for r in chain["structural_risks"]:
        conn.execute(
            "INSERT INTO chain_risks (material_id, kind, risk_type, description, node_keys, source_refs, recorded_at) "
            "VALUES (?, 'risk', ?, ?, ?, ?, ?)",
            (material_id, r["risk_type"], r["description"], _j(r["node_ids"]), _j(r["source_ids"]), now),
        )
    for s in chain["substitutes"]:
        conn.execute(
            "INSERT INTO chain_risks (material_id, kind, title, description, source_refs, recorded_at) "
            "VALUES (?, 'substitute', ?, ?, ?, ?)",
            (material_id, s["name"], s["explanation"], _j(s["source_ids"]), now),
        )
    for q in [*chain["open_questions"], *general]:
        conn.execute(
            "INSERT INTO chain_risks (material_id, kind, description, recorded_at) VALUES (?, 'open_question', ?, ?)",
            (material_id, q, now),
        )
    for d in chain["upcoming_dates"]:
        conn.execute(
            "INSERT INTO chain_dates (material_id, date, what, node_keys, source_refs, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (material_id, d["date"], d["what"], _j(d["node_ids"]), _j(d["source_ids"]), now),
        )
    conn.execute(
        "INSERT INTO chain_reports (material_id, report, injection_suspected, created_at) VALUES (?, ?, ?, ?)",
        (material_id, report, int(bool(chain["injection_suspected"])), now),
    )
    known = json.loads(conn.execute("SELECT hs_codes FROM materials WHERE id = ?", (material_id,)).fetchone()[0])
    official = [h for h in known if h.get("origin") == "ecics"]
    hs = official + [{"code": c, "origin": "ai", "verified": False} for c in dict.fromkeys(
        chain["material"]["hs_code_candidates"]) if isinstance(c, str) and c.isdigit() and len(c) == 6
        and all(h["code"] != c for h in official)]
    conn.execute("UPDATE materials SET hs_codes = ?, status = 'review', updated_at = ? WHERE id = ?",
                 (_j(hs), now, material_id))


SOURCES_OF = {
    "node": "SELECT s.ref FROM node_sources x JOIN sources s ON s.id = x.source_id WHERE x.node_id = ? ORDER BY s.id",
    "edge": "SELECT s.ref FROM edge_sources x JOIN sources s ON s.id = x.source_id WHERE x.edge_id = ? ORDER BY s.id",
}
NODE_COLUMNS = (
    "material_id", "node_key", "node_type", "name", "product", "tier", "country_iso2", "place", "lat", "lon",
    "global_share_pct", "buffer_days", "buffer_is_default", "capacity_text", "confidence", "notes", "anomalies",
    "user_modified", "valid_from", "valid_to", "recorded_at", "coord_source", "added_by",
)
EDGE_COLUMNS = (
    "material_id", "from_key", "to_key", "relation", "transport_mode", "lead_time_days", "lead_time_basis",
    "share_pct", "confidence", "anomalies", "user_modified", "valid_from", "valid_to", "recorded_at",
)
INSERT_NODE = (
    "INSERT INTO chain_nodes (material_id, node_key, node_type, name, product, tier, country_iso2, place, lat, lon, "
    "global_share_pct, buffer_days, buffer_is_default, capacity_text, confidence, notes, anomalies, user_modified, "
    "valid_from, valid_to, recorded_at, coord_source, added_by) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
INSERT_EDGE = (
    "INSERT INTO chain_edges (material_id, from_key, to_key, relation, transport_mode, lead_time_days, "
    "lead_time_basis, share_pct, confidence, anomalies, user_modified, valid_from, valid_to, recorded_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _sources_for(conn: sqlite3.Connection, kind: str, row_id: int) -> list[str]:
    return [r[0] for r in conn.execute(SOURCES_OF[kind], (row_id,)).fetchall()]


def load_chain(conn: sqlite3.Connection, material_id: str) -> dict[str, Any]:
    nodes = []
    for r in conn.execute(
        "SELECT * FROM chain_nodes WHERE material_id = ? AND valid_to IS NULL ORDER BY tier DESC, id", (material_id,)
    ):
        n = dict(r)
        n["anomalies"] = json.loads(n["anomalies"])
        n["source_refs"] = _sources_for(conn, "node", n["id"])
        nodes.append(n)
    edges = []
    for r in conn.execute("SELECT * FROM chain_edges WHERE material_id = ? AND valid_to IS NULL ORDER BY id",
                          (material_id,)):
        e = dict(r)
        e["anomalies"] = json.loads(e["anomalies"])
        e["source_refs"] = _sources_for(conn, "edge", e["id"])
        edges.append(e)
    sources = [dict(r) for r in conn.execute(
        "SELECT ref, url, title, publisher, page_age FROM sources WHERE material_id = ? ORDER BY id", (material_id,)
    )]
    risks = []
    for r in conn.execute("SELECT * FROM chain_risks WHERE material_id = ? ORDER BY id", (material_id,)):
        item = dict(r)
        item["node_keys"] = json.loads(item["node_keys"])
        item["source_refs"] = json.loads(item["source_refs"])
        risks.append(item)
    routes = []
    for r in conn.execute("SELECT * FROM chain_routes WHERE material_id = ? ORDER BY id", (material_id,)):
        item = dict(r)
        item["node_keys"] = json.loads(item["node_keys"])
        item["source_refs"] = json.loads(item["source_refs"])
        routes.append(item)
    dates = []
    for r in conn.execute("SELECT * FROM chain_dates WHERE material_id = ? ORDER BY date", (material_id,)):
        item = dict(r)
        item["node_keys"] = json.loads(item["node_keys"])
        item["source_refs"] = json.loads(item["source_refs"])
        dates.append(item)
    report = conn.execute(
        "SELECT injection_suspected FROM chain_reports WHERE material_id = ? ORDER BY id DESC LIMIT 1", (material_id,)
    ).fetchone()
    return {
        "nodes": nodes, "edges": edges, "sources": sources, "routes": routes, "dates": dates,
        "risks": [r for r in risks if r["kind"] == "risk"],
        "substitutes": [r for r in risks if r["kind"] == "substitute"],
        "open_questions": [r["description"] for r in risks if r["kind"] == "open_question"],
        "injection_suspected": bool(report and report[0]),
    }


def revise_node(conn: sqlite3.Connection, row: sqlite3.Row, changes: dict[str, Any], *, by_user: bool = False
                ) -> None:
    """Closes the current row and inserts its revision (bitemporal), with the same sources."""
    now = utcnow()
    new = {**dict(row), **changes}
    conn.execute("UPDATE chain_nodes SET valid_to = ? WHERE id = ?", (now, row["id"]))
    new.update({"valid_from": now, "valid_to": None, "recorded_at": now})
    if by_user:
        new.update({"user_modified": 1, "anomalies": "[]"})
    cur = conn.execute(INSERT_NODE, [new[c] for c in NODE_COLUMNS])
    conn.execute("INSERT INTO node_sources SELECT ?, source_id FROM node_sources WHERE node_id = ?",
                 (cur.lastrowid, row["id"]))


def correct_node(conn: sqlite3.Connection, material_id: str, node_key: str, changes: dict[str, Any]) -> None:
    """User correction: closes the current row and inserts the corrected one (bitemporal). A position given by the
    user must lie in the node's country (straits excepted); a new place or country is located again."""
    from goulot.chain import geo, places

    unknown = set(changes) - set(EDITABLE_NODE_FIELDS)
    if unknown:
        raise ChainStateError(f"champs non modifiables : {sorted(unknown)}")
    row = conn.execute(
        "SELECT * FROM chain_nodes WHERE material_id = ? AND node_key = ? AND valid_to IS NULL", (material_id, node_key)
    ).fetchone()
    if row is None:
        raise ChainStateError("maillon introuvable")
    changes = dict(changes)
    if "buffer_days" in changes:
        changes["buffer_is_default"] = 0
    new = {**dict(row), **changes}
    if changes.get("lat") is not None and changes.get("lon") is not None:
        if not (-90 <= new["lat"] <= 90 and -180 <= new["lon"] <= 180):
            raise ChainStateError("position invalide")
        found = geo.country_at(new["lat"], new["lon"])
        if (new["node_type"] != "chokepoint" and new["country_iso2"] != "unknown" and found is not None
                and found != new["country_iso2"] and not geo._near(new["lat"], new["lon"], new["country_iso2"], None)):
            raise ChainStateError(f"cette position est en {found}, pas en {new['country_iso2']}")
        changes["coord_source"] = "user"
    elif "lat" in changes or "lon" in changes or (
            {"place", "country_iso2", "name"} & set(changes) and row["coord_source"] != "user"):
        changes.update({"lat": None, "lon": None, "coord_source": ""})
    if changes.get("coord_source") == "":
        lat, lon, source = places.computed_position({**dict(row), **changes})
        changes.update({"lat": lat, "lon": lon, "coord_source": source})
    revise_node(conn, row, changes, by_user=True)
    refresh_rules(conn, material_id)


def correct_edge(conn: sqlite3.Connection, material_id: str, edge_id: int, changes: dict[str, Any]) -> None:
    unknown = set(changes) - set(EDITABLE_EDGE_FIELDS)
    if unknown:
        raise ChainStateError(f"champs non modifiables : {sorted(unknown)}")
    row = conn.execute(
        "SELECT * FROM chain_edges WHERE material_id = ? AND id = ? AND valid_to IS NULL", (material_id, edge_id)
    ).fetchone()
    if row is None:
        raise ChainStateError("lien introuvable")
    now = utcnow()
    new = {**dict(row), **changes}
    if "lead_time_days" in changes:
        new["lead_time_basis"] = "user"
    conn.execute("UPDATE chain_edges SET valid_to = ? WHERE id = ?", (now, row["id"]))
    new.update({"user_modified": 1, "valid_from": now, "valid_to": None, "recorded_at": now, "anomalies": "[]"})
    cur = conn.execute(INSERT_EDGE, [new[c] for c in EDGE_COLUMNS])
    conn.execute("INSERT INTO edge_sources SELECT ?, source_id FROM edge_sources WHERE edge_id = ?",
                 (cur.lastrowid, row["id"]))


def validate_material(conn: sqlite3.Connection, material_id: str, *, acknowledge_injection: bool) -> None:
    material = get_material(conn, material_id)
    if material is None:
        raise ChainStateError("matière inconnue")
    if material["status"] != "review":
        raise ChainStateError("seule une chaîne reconstruite, en relecture, peut être validée")
    flagged = conn.execute(
        "SELECT injection_suspected FROM chain_reports WHERE material_id = ? ORDER BY id DESC LIMIT 1", (material_id,)
    ).fetchone()
    if flagged and flagged[0] and not acknowledge_injection:
        raise ChainStateError("contenu piégé signalé : relisez les sources et confirmez avant de valider")
    now = utcnow()
    conn.execute("UPDATE materials SET status = 'validated', validated_at = ?, updated_at = ? WHERE id = ?",
                 (now, now, material_id))
    refresh_rules(conn, material_id)
    from goulot.prices.collect import propose_links

    propose_links(conn, material_id)


def refresh_rules(conn: sqlite3.Connection, material_id: str) -> int:
    """Watch rules of a validated chain (step 7 « Armer la veille »); nothing is watched before validation."""
    from goulot.chain.places import apply_routes
    from goulot.collect.rules import generate

    material = get_material(conn, material_id)
    if material is None or material["status"] != "validated":
        return 0
    apply_routes(conn, material_id)
    return generate(conn, material, load_chain(conn, material_id)["nodes"])
