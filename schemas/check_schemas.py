"""Check the JSON schemas against Claude structured-output constraints and validate the examples.

Usage: uv run python schemas/check_schemas.py   (needs the jsonschema package)

The business checks below must also run on every real model output (goulot/llm/validate.py).
Turn this script into tests/test_schemas.py during phase 1.
"""

import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
UNSUPPORTED = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "maxItems", "uniqueItems", "patternProperties",
    "minProperties", "maxProperties", "if", "then", "else", "not", "dependentRequired",
}
MAX_OPTIONAL, MAX_UNION = 24, 16
DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
ISO2 = re.compile(r"^[A-Z]{2}$")
errors: list[str] = []


def fail(where: str, msg: str) -> None:
    errors.append(f"{where}: {msg}")


def words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w not in {"", ":", ";", "—", "-"}])


def check_compat(name: str, schema: dict) -> None:
    optional = unions = 0
    defs = schema.get("$defs", {})

    def walk(node, path):
        nonlocal optional, unions
        if isinstance(node, dict):
            for key in node:
                if key in UNSUPPORTED:
                    fail(name, f"{path}: unsupported keyword {key}")
            if node.get("minItems", 0) not in (0, 1):
                fail(name, f"{path}: minItems must be 0 or 1")
            ref = node.get("$ref")
            if ref and not (ref.startswith("#/$defs/") and ref.split("/")[-1] in defs):
                fail(name, f"{path}: unresolved $ref {ref}")
            if node.get("type") == "object" or "properties" in node:
                if node.get("additionalProperties") is not False:
                    fail(name, f"{path}: additionalProperties must be false")
                props = node.get("properties", {})
                required = set(node.get("required", []))
                if required - set(props):
                    fail(name, f"{path}: required lists unknown fields {sorted(required - set(props))}")
                optional += len([p for p in props if p not in required])
                unions += sum(1 for sub in props.values() if isinstance(sub.get("type"), list) or "anyOf" in sub)
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}/{i}")

    walk(schema, "#")
    if optional > MAX_OPTIONAL:
        fail(name, f"{optional} optional fields (limit {MAX_OPTIONAL})")
    if unions > MAX_UNION:
        fail(name, f"{unions} union-type fields (limit {MAX_UNION})")
    print(f"{name}: optional fields {optional}/{MAX_OPTIONAL}, union fields {unions}/{MAX_UNION}")


def check_triage(ex: dict, chain_ids: set[str]) -> None:
    w = "triage example"
    if words(ex["title_fr"]) > 12:
        fail(w, "title_fr longer than 12 words")
    if words(ex["summary_fr"]) > 30:
        fail(w, "summary_fr longer than 30 words")
    if ex["country_iso2"] != "unknown" and not ISO2.match(ex["country_iso2"]):
        fail(w, "bad country_iso2")
    if ex["event_date"] != "unknown" and not DATE.match(ex["event_date"]):
        fail(w, "bad event_date")
    for d in ex["future_dates"]:
        if not DATE.match(d["date"]):
            fail(w, f"bad future date {d['date']}")
    unknown = set(ex["matched_node_ids"]) - chain_ids
    if unknown:
        fail(w, f"matched_node_ids not in the chain: {sorted(unknown)}")


def check_chain(ex: dict, allowed_sources: set[str], expected_cas: str) -> set[str]:
    w = "supply_chain example"
    nodes = {n["node_id"]: n for n in ex["nodes"]}
    if len(nodes) != len(ex["nodes"]):
        fail(w, "duplicate node_id")
    if ex["material"]["cas"] != expected_cas:
        fail(w, "material.cas differs from the requested CAS")
    out_edges: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in ex["edges"]:
        if e["from_id"] not in nodes or e["to_id"] not in nodes:
            fail(w, f"edge {e['from_id']} -> {e['to_id']} uses an unknown node")
            continue
        if e["from_id"] == e["to_id"]:
            fail(w, f"self-loop on {e['from_id']}")
        out_edges[e["from_id"]].append(e["to_id"])
        if e["share_pct"] is not None and not 0 <= e["share_pct"] <= 100:
            fail(w, "edge share_pct out of 0-100")
        if e["lead_time_days"] is not None and e["lead_time_days"] < 0:
            fail(w, "negative lead_time_days")
    sinks = [nid for nid, outs in out_edges.items() if not outs]
    tier0 = [nid for nid, n in nodes.items() if n["tier"] == 0]
    if len(tier0) != 1 or nodes[tier0[0]]["node_type"] != "material":
        fail(w, "there must be exactly one tier-0 node, of type material")
    elif sinks != tier0:
        fail(w, f"the purchased material must be the only node without outgoing edge (sinks: {sinks})")
    else:
        sink = tier0[0]
        for nid in nodes:  # every node must reach the purchased material
            seen, stack = set(), [nid]
            while stack:
                cur = stack.pop()
                if cur == sink:
                    break
                if cur not in seen:
                    seen.add(cur)
                    stack.extend(out_edges[cur])
            else:
                fail(w, f"{nid} has no path to {sink}")
    state: dict[str, int] = {}

    def has_cycle(nid: str) -> bool:
        state[nid] = 1
        for nxt in out_edges.get(nid, []):
            if state.get(nxt) == 1 or (state.get(nxt) is None and has_cycle(nxt)):
                return True
        state[nid] = 2
        return False

    if any(state.get(nid) is None and has_cycle(nid) for nid in nodes):
        fail(w, "the graph has a cycle (flag the chain for review)")
    for n in ex["nodes"]:
        nid = n["node_id"]
        if not re.match(r"^[a-z0-9_]+$", nid):
            fail(w, f"bad node_id {nid}")
        if (n["lat"] is None) != (n["lon"] is None):
            fail(w, f"{nid}: lat and lon must both be set or both null")
        if n["lat"] is not None and not (-90 <= n["lat"] <= 90 and -180 <= n["lon"] <= 180):
            fail(w, f"{nid}: coordinates out of range")
        if n["node_type"] == "country" and n["lat"] is not None:
            fail(w, f"{nid}: no coordinates for a whole country")
        if n["global_share_pct"] is not None and not 0 <= n["global_share_pct"] <= 100:
            fail(w, f"{nid}: global_share_pct out of 0-100")
        if n["country_iso2"] != "unknown" and not ISO2.match(n["country_iso2"]):
            fail(w, f"{nid}: bad country_iso2")
    for item in ex["nodes"] + ex["edges"]:
        if item["confidence"] in ("high", "medium") and not item["source_ids"]:
            fail(w, f"{item.get('node_id') or item['from_id']}: high or medium confidence without source")
    referenced = [s for part in ("routes", "nodes", "edges", "substitutes", "structural_risks", "upcoming_dates")
                  for item in ex[part] for s in item["source_ids"]]
    if set(referenced) - allowed_sources:
        fail(w, f"source_ids not provided in the input: {sorted(set(referenced) - allowed_sources)}")
    for part in ("routes", "structural_risks", "upcoming_dates"):
        for item in ex[part]:
            missing = set(item["node_ids"]) - set(nodes)
            if missing:
                fail(w, f"{part} references unknown nodes {sorted(missing)}")
    for d in ex["upcoming_dates"]:
        if not DATE.match(d["date"]):
            fail(w, f"bad upcoming date {d['date']}")
    for r in ex["routes"]:
        if r["global_share_pct"] is not None and not 0 <= r["global_share_pct"] <= 100:
            fail(w, "route global_share_pct out of 0-100")
    return set(nodes)


def check_impact(ex: dict, dossier: dict[str, set[str]]) -> None:
    """dossier: identifiers actually sent in the request (event, sources, references, instruments, commodities)."""
    w = "impact example"
    if ex["event_id"] not in dossier["events"]:
        fail(w, "event_id not in the dossier")
    if len(ex["hypotheses"]) > 4:
        fail(w, "more than 4 hypotheses")
    for h in ex["hypotheses"]:
        if not 0 <= h["probability"] <= 1 or h["move_p10"] > h["move_p90"]:
            fail(w, f"hypothesis {h['hypothesis_id']} inconsistent")
        if h["instrument_id"] not in dossier["instruments"]:
            fail(w, f"unknown instrument {h['instrument_id']}")
    if not 0 <= ex["confidence"]["analysis_confidence"] <= 1:
        fail(w, "analysis_confidence out of 0-1")
    understanding = ex["event_understanding"]
    cited = [s for item in understanding["facts"] + understanding["contradictions"] for s in item["source_ids"]]
    if set(cited) - dossier["sources"]:
        fail(w, f"unknown source ids {sorted(set(cited) - dossier['sources'])}")
    refs = [e["reference_id"] for e in understanding["affected_entities"] if e["reference_id"] != "unmapped"]
    refs += [r for ci in ex["commodity_impacts"] for r in ci["supply_at_risk"]["reference_ids"]]
    if set(refs) - dossier["references"]:
        fail(w, f"unknown reference ids {sorted(set(refs) - dossier['references'])}")
    for ci in ex["commodity_impacts"]:
        if ci["commodity"] not in dossier["commodities"]:
            fail(w, f"unknown commodity {ci['commodity']}")
        d = ci["expected_duration_days"]
        if d["low"] is not None and d["high"] is not None and d["low"] > d["high"]:
            fail(w, "duration low > high")
        share = ci["supply_at_risk"]["global_share_pct"]
        if share is not None and not 0 <= share <= 100:
            fail(w, "global_share_pct out of 0-100")
    severities = [ci["severity"] for ci in ex["commodity_impacts"]]
    if ex["assessment_status"] == "no_material_impact" and any(severities):
        fail(w, "no_material_impact requires severity 0 everywhere")
    if ex["assessment_status"] == "impact_assessed" and not any(severities):
        fail(w, "impact_assessed with every severity at 0 should be no_material_impact")


def check_digest(ex: dict) -> None:
    w = "digest example"
    if words(ex["headline"]) > 12:
        fail(w, "headline longer than 12 words")
    if not 1 <= len(ex["what_changed"]) <= 3 or any(words(x) > 25 for x in ex["what_changed"]):
        fail(w, "what_changed must have 1 to 3 items of 25 words at most")
    if words(ex["verdict_explanation"]) > 50:
        fail(w, "verdict_explanation longer than 50 words")
    if len(ex["watch_next"]) > 3:
        fail(w, "more than 3 watch_next items")
    for item in ex["watch_next"]:
        if item["date"] != "unknown" and not DATE.match(item["date"]):
            fail(w, f"bad watch_next date {item['date']}")
    if ex["disagrees_with_verdict"] != bool(ex["disagreement_reason"].strip()):
        fail(w, "disagreement_reason must be filled if and only if disagrees_with_verdict is true")


def check_cases(ex: dict, allowed_sources: set[str], expected_cas: str) -> None:
    w = "historical_case example"
    if ex["material_cas"] != expected_cas:
        fail(w, "material_cas differs from the requested CAS")
    ids = [c["case_id"] for c in ex["cases"]]
    if len(ids) != len(set(ids)):
        fail(w, "duplicate case_id")
    for c in ex["cases"]:
        cid = c["case_id"]
        if not re.match(r"^[a-z0-9_]+$", cid):
            fail(w, f"bad case_id {cid}")
        if not DATE.match(c["first_report_date"]):
            fail(w, f"{cid}: bad first_report_date")
        if c["end_date"] != "unknown" and not DATE.match(c["end_date"]):
            fail(w, f"{cid}: bad end_date")
        if len(c["first_report_date"]) == 10 and len(c["end_date"]) == 10 and c["end_date"] < c["first_report_date"]:
            fail(w, f"{cid}: end_date before first_report_date")
        if c["country_iso2"] != "unknown" and not ISO2.match(c["country_iso2"]):
            fail(w, f"{cid}: bad country_iso2")
        if c["capacity_share_pct"] is not None and not 0 <= c["capacity_share_pct"] <= 100:
            fail(w, f"{cid}: capacity_share_pct out of 0-100")
        if c["capacity_share_pct"] is not None and c["capacity_share_basis"] == "unknown":
            fail(w, f"{cid}: capacity_share_pct without basis")
        if c["duration_days"] is not None and c["duration_days"] < 0:
            fail(w, f"{cid}: negative duration_days")
        unknown = set(c["source_ids"]) - allowed_sources
        if unknown:
            fail(w, f"{cid}: source_ids not provided in the input: {sorted(unknown)}")


def main() -> int:
    schemas, examples = {}, {}
    for path in sorted(HERE.glob("*.json")):
        name = path.stem
        schemas[name] = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schemas[name])
        check_compat(name, schemas[name])
        ex_path = HERE / "examples" / f"{name}.example.json"
        if not ex_path.exists():
            fail(name, "missing example")
            continue
        examples[name] = json.loads(ex_path.read_text(encoding="utf-8"))
        for err in Draft202012Validator(schemas[name]).iter_errors(examples[name]):
            fail(f"{name} example", f"{list(err.path)} {err.message}")
    if errors:  # stop before business checks if the examples do not even match their schemas
        print("\n".join(errors))
        return 1
    chain_ids = check_chain(examples["supply_chain_v1"], {f"src{i}" for i in range(1, 5)}, "107-13-1")
    check_triage(examples["triage_v1"], chain_ids)
    check_impact(examples["impact_assessment_v1"], {
        "events": {"EVT-2026-10-03-0142"},
        "sources": {"S1", "S2", "S3"},
        "references": {f"R{i}" for i in range(1, 7)},
        "instruments": {"LME_CU_3M", "LME_CU_CASH_3M"},
        "commodities": {"copper"},
    })
    check_digest(examples["digest_v1"])
    check_cases(examples["historical_case_v1"], {"src1", "src2", "src3"}, "107-13-1")
    print("\n".join(errors) if errors else "All schemas and examples OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
