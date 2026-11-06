"""Compliance report (docs/SPEC.md, section 10): assembled by code from validated data, without AI, without network.

The report is a JSON document. Its fingerprint is the SHA-256 of its canonical form without the `fingerprint` and
`ledger_seq` fields: anyone can recompute it from the JSON annex. The PDF is the browser's print of the same
document; the CSV annex is a flat view of it.

Private inputs (Ma chaîne, coverage, mitigation choices, signatory saved in the vault) arrive already decrypted, only
through `/api/private/` with the vault open, and only in memory: nothing here writes or logs them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from goulot.chain import store
from goulot.chain.countries import name_fr
from goulot.chain.delays import delays_to_material
from goulot.compliance import concentration as conc
from goulot.compliance.templates import MITIGATION_LABELS, strategic_match
from goulot.ledger import outcomes

CONFIDENTIAL_LABEL = "Confidentiel : fondé sur Ma chaîne"
EVENT_DAYS = 365
VERDICT_LABELS = {"ras": "Rien à signaler", "pot": "Problème potentiel", "ant": "Anticiper stock",
                  "unverified": "Non vérifié"}
CONFIDENCE_LABELS = {"high": "élevée", "medium": "moyenne", "low": "faible"}
TYPE_LABELS = {"mine": "mine", "plant": "usine", "recycler": "recycleur", "country": "pays", "company": "entreprise",
               "other": "autre"}
ROLE_LABELS = {"producer": "producteur", "processor": "transformateur", "distributor": "distributeur",
               "warehouse": "entrepôt", "port": "port"}
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
RANK_CONFIDENCE = {v: k for k, v in CONFIDENCE_RANK.items()}
TRADE_RISKS = {"regulatory"}
OTHER_RISKS = {"geographic_concentration", "single_supplier", "seasonality", "recurring_maintenance", "other"}
CHECK_LABELS = {"mapping": "Cartographie", "factors": "Facteurs de risque", "vulnerability": "Vulnérabilité",
                "mitigation": "Mesures d'atténuation", "chain_date": "Chaîne validée depuis moins de 12 mois",
                "my_chain": "Ma chaîne", "validation": "Validation"}

METHOD = (
    "Cartographie : maillons de la chaîne validée (mines, usines, recycleurs et pays), avec leurs parts de la "
    "production mondiale, leurs sources et leur niveau de confiance ; une étape absente de la chaîne est signalée "
    "comme non documentée. Concentration : part du premier pays et indice de Herfindahl-Hirschman (somme des carrés "
    "des parts nationales, de 0 à 10 000 ; faible sous 1 500, forte au-delà de 2 500), par étape. Les parts ne sont "
    "jamais ramenées à 100 % : quand elles couvrent moins, l'indice est un minimum. Repère européen de 65 % "
    "(règlement (UE) 2024/1252, article 5.1.b) : objectif de l'Union pour 2030, qui vise un seul pays tiers ; il est "
    "appliqué ici aux parts mondiales de chaque étape{my_chain}, en repère seulement. "
    "Délai de propagation : temps de parcours le plus court d'un producteur jusqu'à la matière achetée, le long de "
    "la chaîne validée. Aucune intelligence artificielle n'a rédigé ce rapport ni calculé ses chiffres. Empreinte : "
    "SHA-256 du contenu de l'annexe JSON, sans ses champs « fingerprint » et « ledger_seq »."
)


@dataclass
class PrivateInputs:
    """Decrypted private inputs, held in memory for one request (vault open)."""
    measures: dict[str, dict[str, Any]] = field(default_factory=dict)
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    my_chains: dict[str, Any] = field(default_factory=dict)  # material_id -> mychain.Model


@dataclass
class Request:
    template: dict[str, Any]
    material_ids: list[str]
    basis: str = "typical"
    company: str = ""
    signatory_name: str = ""
    signatory_role: str = ""
    reviewed: bool = False


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(report: dict[str, Any]) -> str:
    body = {k: v for k, v in report.items() if k not in ("fingerprint", "ledger_seq")}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def _months_between(earlier: date, later: date) -> float:
    return (later - earlier).days / 30.44


def _date_reached(value: str, today: date) -> bool:
    """« 2027 », « 2027-01 » or « 2027-01-15 » still to come (or in progress)."""
    iso = today.isoformat()
    return value >= iso[: len(value)]


# --- one material ----------------------------------------------------------------------------------------------
def _mapping(chain: dict[str, Any], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for stage in conc.STAGES:
        levels = []
        for g in (g for g in groups if g["stage"] == stage):
            sites = [{"name": n["name"], "type": TYPE_LABELS.get(n["node_type"], n["node_type"]),
                      "country": n["country_iso2"], "country_name": name_fr(n["country_iso2"])
                      if n["country_iso2"] != "unknown" else "inconnu", "place": n["place"] if n["place"] != "unknown"
                      else "", "share_pct": n["global_share_pct"],
                      "confidence": CONFIDENCE_LABELS[n["confidence"]], "sources": n["source_refs"]}
                     for n in g["nodes"]]
            shares = sorted(g["shares"].items(), key=lambda kv: (-kv[1], kv[0]))
            ranked = [CONFIDENCE_RANK[n["confidence"]] for n in g["nodes"]]
            levels.append({
                "tier": g["tier"], "products": g["products"],
                "countries": [{"country": c, "country_name": name_fr(c), "share_pct": round(v, 1)} for c, v in shares],
                "sites": sites, "confidence": CONFIDENCE_LABELS[RANK_CONFIDENCE[min(ranked)]] if ranked else None,
                "sources": sorted({s for n in g["nodes"] for s in n["source_refs"]}),
            })
        out.append({"stage": stage, "label": conc.STAGE_LABELS[stage], "documented": bool(levels),
                    "levels": levels})
    return out


def _concentration(groups: list[dict[str, Any]], template: dict[str, Any]) -> list[dict[str, Any]]:
    bench = float(template["benchmarks"]["single_country_share_max_pct"])
    bands = template["benchmarks"]["hhi_bands"]
    rows = []
    for g in groups:
        if not g["shares"]:
            continue
        rows.append({"stage": g["stage"], "stage_label": conc.STAGE_LABELS[g["stage"]], "tier": g["tier"],
                     "products": g["products"], **conc.figures(g["shares"], bench, bands)})
    return rows


def _risk(r: dict[str, Any]) -> dict[str, Any]:
    return {"title": r.get("title") or "", "description": r["description"], "sources": r["source_refs"]}


def _factors(conn: sqlite3.Connection, material_id: str, chain: dict[str, Any], today: date) -> dict[str, Any]:
    risks = chain["risks"]
    since = (today - timedelta(days=EVENT_DAYS)).isoformat()
    events = [{"id": r["id"], "title": r["title"], "place": r["place"], "country": r["country_iso2"],
               "first_seen": r["first_seen_at"][:10], "status": r["event_status"],
               "confirmed": r["confirmation"] == "confirmed"}
              for r in conn.execute(
                  "SELECT id, title, place, country_iso2, first_seen_at, event_status, confirmation FROM public_events "
                  "WHERE material_id = ? AND first_seen_at >= ? ORDER BY first_seen_at DESC LIMIT 40",
                  (material_id, since))]
    passages = [{"name": n["name"], "type": n["node_type"], "country": n["country_iso2"]}
                for n in chain["nodes"] if n["node_type"] in ("chokepoint", "waterway")]
    return {
        "export_controls": [_risk(r) for r in risks if r["risk_type"] == "export_control"],
        "trade_measures": [_risk(r) for r in risks if r["risk_type"] in TRADE_RISKS],
        "chokepoints": {"passages": passages,
                        "risks": [_risk(r) for r in risks if r["risk_type"] == "logistics_chokepoint"]},
        "coproduct_dependencies": [_risk(r) for r in risks if r["risk_type"] == "coproduct_dependency"],
        "other_structural": [_risk(r) for r in risks if r["risk_type"] in OTHER_RISKS],
        "events_12_months": events,
        "upcoming_bottlenecks": [{"date": d["date"], "what": d["what"], "sources": d["source_refs"]}
                                 for d in chain["dates"] if _date_reached(str(d["date"]), today)],
    }


def _vulnerability(conn: sqlite3.Connection, material_id: str, chain: dict[str, Any], groups: list[dict[str, Any]],
                   private: PrivateInputs | None) -> dict[str, Any]:
    v = conn.execute("SELECT verdict, x_weeks, x_low, x_high, created_at FROM verdicts WHERE material_id = ? AND "
                     "scope = 'market' AND current = 1", (material_id,)).fetchone()
    verdict = None
    if v is not None:
        verdict = {"verdict": v["verdict"], "label": VERDICT_LABELS[v["verdict"]], "at": v["created_at"][:10],
                   "x_weeks": v["x_weeks"] if v["verdict"] in ("pot", "ant") else None,
                   "x_low": v["x_low"] if v["verdict"] in ("pot", "ant") else None,
                   "x_high": v["x_high"] if v["verdict"] in ("pot", "ant") else None}
    delays = delays_to_material(chain["nodes"], chain["edges"])
    producers = [n for n in chain["nodes"] if int(n["tier"]) == 1 and conc.producing(n) and n["node_key"] in delays]
    propagation = None
    if producers:
        fastest = min(producers, key=lambda n: delays[n["node_key"]]["days"])
        d = delays[fastest["node_key"]]
        propagation = {"weeks": round(d["days"] / 7, 1), "low_weeks": round(d["low"] / 7, 1),
                       "high_weeks": round(d["high"] / 7, 1), "buffers_days": d.get("buffers", 0.0),
                       "from": fastest["name"]}
    buffers = [{"name": n["name"], "days": n["buffer_days"]} for n in chain["nodes"]
               if n.get("buffer_days") and not n.get("buffer_is_default")]
    level = next((g for g in groups if g["tier"] == 1 and g["shares"]), None)
    alternatives = []
    if level is not None:
        ranked = sorted(level["shares"].items(), key=lambda kv: (-kv[1], kv[0]))
        alternatives = [{"country": c, "country_name": name_fr(c), "share_pct": round(s, 1)}
                        for c, s in ranked[1:] if s >= 1]
    documented = bool(chain["routes"] or chain["risks"] or chain["substitutes"] or chain["dates"])
    if private is None:
        cover: dict[str, Any] = {"status": "coffre fermé"}
    else:
        found = private.coverage.get(material_id)
        cover = {"status": "renseignée", **{k: found.get(k) for k in ("stock_weeks", "supplier_lead_time_days")}} \
            if found else {"status": "non saisie"}
    return {
        "verdict": verdict, "propagation": propagation, "buffers": buffers,
        "substitutes": [_risk(s) for s in chain["substitutes"]], "context_documented": documented,
        "alternative_sources": alternatives, "company_coverage": cover,
    }


def _my_chain(model: Any, template: dict[str, Any]) -> dict[str, Any]:
    from goulot.private.mychain import my_country_shares

    bench = float(template["benchmarks"]["single_country_share_max_pct"])
    sites = []
    for s in model.doc.sites:
        sites.append({"name": s.name, "role": ROLE_LABELS.get(s.role, s.role), "company": s.company, "city": s.city,
                      "country": s.country_iso2, "country_name": name_fr(s.country_iso2) if s.country_iso2 else "",
                      "exposure_pct": round(100 * model.exposure.get(s.id, 0.0), 1), "stock_days": s.stock_days,
                      "alternatives": [{"name": a.name, "status": a.status, "switch_weeks": a.switch_weeks}
                                       for a in s.alternatives]})
    sites.sort(key=lambda x: -x["exposure_pct"])
    return {"sites": sites,
            "concentration": conc.figures(my_country_shares(model), bench, template["benchmarks"]["hhi_bands"])}


def _measures(material_id: str, private: PrivateInputs | None) -> dict[str, Any]:
    if private is None:
        return {"status": "coffre fermé", "choices": [], "comment": ""}
    found = private.measures.get(material_id) or {}
    choices = [c for c in found.get("choices", []) if c in MITIGATION_LABELS]
    return {"status": "renseignées" if choices else "à choisir",
            "choices": [{"id": c, "label": MITIGATION_LABELS[c]} for c in choices],
            "comment": str(found.get("comment") or "")}


def _material(conn: sqlite3.Connection, material_id: str, req: Request, today: date,
              private: PrivateInputs | None) -> dict[str, Any]:
    m = store.get_material(conn, material_id)
    if m is None:
        raise LookupError(material_id)
    chain = store.load_chain(conn, material_id)
    groups = conc.stage_groups(chain["nodes"])
    concentration = _concentration(groups, req.template)
    worst = max(concentration, key=lambda r: (r["top_share_pct"] or 0, r["hhi"] or 0), default=None)
    out: dict[str, Any] = {
        "id": material_id, "name": m["name"], "cas": m["cas"], "status": m["status"],
        "chain_validated_at": (m.get("validated_at") or "")[:10] or None,
        "strategic": strategic_match([m["name"], *m.get("synonyms", [])]),
        "mapping": _mapping(chain, groups),
        "concentration": concentration,
        "most_concentrated": worst,
        "factors": _factors(conn, material_id, chain, today),
        "vulnerability": _vulnerability(conn, material_id, chain, groups, private),
        "mitigation": _measures(material_id, private),
        "sources": [{"ref": s["ref"], "url": s["url"], "title": s["title"], "publisher": s["publisher"],
                     "date": s["page_age"]} for s in chain["sources"]],
    }
    if req.basis == "my_chain":
        model = private.my_chains.get(material_id) if private else None
        out["my_chain"] = _my_chain(model, req.template) if model is not None else None
    out["checks"] = _material_checks(out, req, today)
    return out


def _material_checks(mat: dict[str, Any], req: Request, today: date) -> dict[str, dict[str, Any]]:
    max_age = float(req.template.get("chain_max_age_months", 12))
    validated = mat["chain_validated_at"]
    age_ok = bool(validated) and mat["status"] == "validated" and _months_between(
        date.fromisoformat(validated), today) <= max_age
    with_shares = [lvl for st in mat["mapping"] for lvl in st["levels"] if lvl["countries"]]
    vul = mat["vulnerability"]
    has_delay = bool((vul["verdict"] or {}).get("x_weeks") is not None or vul["propagation"])
    measures = mat["mitigation"]
    other_ok = all(c["id"] != "autre" for c in measures["choices"]) or bool(measures["comment"].strip())
    checks = {
        "mapping": {"ok": bool(with_shares) and bool(mat["sources"]),
                    "detail": "parts par pays et sources" if with_shares else "aucune part par pays dans la chaîne"},
        "factors": {"ok": bool(mat["concentration"]),
                    "detail": "concentration calculée" if mat["concentration"] else "concentration incalculable"},
        "vulnerability": {"ok": has_delay and vul["context_documented"],
                          "detail": "délai et substituts documentés" if has_delay and vul["context_documented"]
                          else "délai ou substituts non documentés"},
        "mitigation": {"ok": bool(measures["choices"]) and other_ok,
                       "detail": ", ".join(c["label"].lower() for c in measures["choices"]) if measures["choices"]
                       else ("coffre fermé" if measures["status"] == "coffre fermé" else "à choisir")},
        "chain_date": {"ok": age_ok, "detail": f"validée le {validated}" if validated else "chaîne non validée"},
    }
    if not other_ok:
        checks["mitigation"]["detail"] = "« Autre » demande un commentaire"
    if req.basis == "my_chain":
        checks["my_chain"] = {"ok": mat.get("my_chain") is not None,
                              "detail": "saisie" if mat.get("my_chain") is not None else "Ma chaîne non saisie"}
    return checks


# --- the report --------------------------------------------------------------------------------------------------
def _reliability(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    ind = outcomes.indicators(conn, today)
    w = ind["warnings_confirmed"]
    if ind["break_in"]:
        text = f"en rodage : {w['good']} avertissement(s) confirmé(s) sur {w['resolved']} jugé(s)"
    else:
        text = f"{w['good']} avertissement(s) confirmé(s) sur {w['resolved']} jugé(s)"
    return {"text": text, "break_in": ind["break_in"], "warnings_good": w["good"], "warnings_resolved": w["resolved"],
            "false_alarms": ind["false_alarms"], "missed_rises": ind["missed_rises"], "since": ind["since"]}


def build(conn: sqlite3.Connection, req: Request, now: datetime, private: PrivateInputs | None = None
          ) -> dict[str, Any]:
    """The whole report, with its completeness checks and its fingerprint (no ledger entry: see `store`)."""
    if req.basis == "my_chain" and private is None:
        raise PermissionError("Ma chaîne exige le coffre ouvert.")
    today = now.date()
    tpl = req.template
    materials = [_material(conn, mid, req, today, private) for mid in req.material_ids]
    checks = []
    for key in ("mapping", "factors", "vulnerability", "mitigation", "chain_date", "my_chain"):
        rows = [(m["name"], m["checks"][key]) for m in materials if key in m["checks"]]
        if not rows:
            continue
        bad = [f"{name} : {c['detail']}" for name, c in rows if not c["ok"]]
        checks.append({"id": key, "label": CHECK_LABELS[key], "ok": not bad,
                       "detail": "; ".join(bad) if bad else f"{len(rows)} matière(s)"})
    signed = bool(req.signatory_name.strip() and req.signatory_role.strip())
    checks.append({"id": "validation", "label": CHECK_LABELS["validation"], "ok": signed and req.reviewed,
                   "detail": "rapport relu et validé" if signed and req.reviewed else
                   ("relecture à cocher" if signed else "nom et fonction du signataire à saisir")})
    if not materials:
        checks.insert(0, {"id": "materials", "label": "Matières", "ok": False, "detail": "aucune matière choisie"})
    draft = not all(c["ok"] for c in checks)
    sections = {s["id"]: s["title"] for s in tpl["sections"]}
    report: dict[str, Any] = {
        "format": "goulot-compliance-1",
        "template": {"id": tpl["id"], "version": tpl["version"], "title": tpl["title"],
                     "subtitle": tpl.get("subtitle", ""), "reference": tpl["reference"],
                     "disclaimer": " ".join(str(tpl["disclaimer"]).split()), "sections": sections},
        "generated_at": now.isoformat(timespec="seconds"),
        "basis": req.basis,
        "confidential": req.basis == "my_chain",
        "confidential_label": CONFIDENTIAL_LABEL if req.basis == "my_chain" else None,
        "draft": draft,
        "draft_label": tpl["draft_label"] if draft else None,
        "company": req.company.strip(),
        "signatory": {"name": req.signatory_name.strip(), "role": req.signatory_role.strip()},
        "reviewed": req.reviewed,
        "benchmarks": tpl["benchmarks"],
        "materials": materials,
        "method": METHOD.format(my_chain=", et à la répartition de Ma chaîne" if req.basis == "my_chain" else ""),
        "reliability": _reliability(conn, today),
        "checks": checks,
    }
    report["fingerprint"] = fingerprint(report)
    return report


# --- annexes -----------------------------------------------------------------------------------------------------
CSV_COLUMNS = ["matiere", "cas", "section", "etape", "niveau", "element", "pays", "part_pct", "valeur", "sources"]


def _cell(value: Any) -> str:
    """A text a spreadsheet would run as a formula is neutralized."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "@", "\t", "\r") or (text[:1] in ("+", "-") and not text[1:2].isdigit()):
        return "'" + text
    return text


def annex_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def annex_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)

    def row(m: dict[str, Any], *cells: Any) -> None:
        w.writerow([_cell(x) for x in (m["name"], m["cas"], *cells)])

    for m in report["materials"]:
        for st in m["mapping"]:
            if not st["documented"]:
                row(m, "cartographie", st["label"], "", "étape non documentée", "", "", "", "")
            for lvl in st["levels"]:
                for c in lvl["countries"]:
                    row(m, "cartographie", st["label"], lvl["tier"], ", ".join(lvl["products"]), c["country_name"],
                        c["share_pct"], f"confiance {lvl['confidence']}", " ".join(lvl["sources"]))
                for s in lvl["sites"]:
                    if s["type"] != "pays":
                        row(m, "cartographie", st["label"], lvl["tier"], s["name"], s["country_name"],
                            s["share_pct"] if s["share_pct"] is not None else "", f"{s['type']}, confiance "
                            f"{s['confidence']}", " ".join(s["sources"]))
        for r in m["concentration"]:
            b = r["benchmark"]
            row(m, "concentration", r["stage_label"], r["tier"], ", ".join(r["products"]), r["top_country_name"],
                r["top_share_pct"], f"HHI {r['hhi']}{' (minimum)' if r['lower_bound'] else ''} ; repère 65 % : "
                f"{b['status']}", "")
        if m.get("my_chain"):
            for s in m["my_chain"]["sites"]:
                row(m, "ma_chaine", s["role"], "", s["name"], s["country_name"], s["exposure_pct"], s["city"], "")
        f = m["factors"]
        for key, label in (("export_controls", "contrôle d'exportation"), ("trade_measures", "mesure commerciale"),
                           ("coproduct_dependencies", "dépendance de coproduit"),
                           ("other_structural", "risque structurel")):
            for r in f[key]:
                row(m, "facteurs", label, "", r["title"], "", "", r["description"], " ".join(r["sources"]))
        for r in f["chokepoints"]["risks"]:
            row(m, "facteurs", "détroit ou voie", "", r["title"], "", "", r["description"], " ".join(r["sources"]))
        for e in f["events_12_months"]:
            row(m, "facteurs", "événement", "", e["title"], e["country"], "", e["first_seen"], "")
        for d in f["upcoming_bottlenecks"]:
            row(m, "facteurs", "échéance", "", d["what"], "", "", d["date"], " ".join(d["sources"]))
        vul = m["vulnerability"]
        if vul["propagation"]:
            row(m, "vulnerabilite", "délai de propagation", "", vul["propagation"]["from"], "", "",
                f"{vul['propagation']['weeks']} sem.", "")
        for s in vul["substitutes"]:
            row(m, "vulnerabilite", "substitut", "", s["title"], "", "", s["description"], " ".join(s["sources"]))
        for c in m["mitigation"]["choices"]:
            row(m, "mesures", "", "", c["label"], "", "", m["mitigation"]["comment"] if c["id"] == "autre" else "",
                "")
        for s in m["sources"]:
            row(m, "sources", "", "", s["title"] or s["publisher"], "", "", s["url"], s["ref"])
    w.writerow([_cell(x) for x in ("empreinte", "", "", "", "", "", "", "", report["fingerprint"],
                                   f"registre #{report.get('ledger_seq', '')}")])
    return "﻿" + buf.getvalue()
