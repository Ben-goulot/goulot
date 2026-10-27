"""`goulot dev record --cas <CAS>`: real reconstruction of one of the 3 test CAS, AI answers recorded in
`tests/fixtures/llm/<CAS>/` for the offline tests (CLAUDE.md, Budget). Run by the user in their own Terminal.

Never for the user's own CAS: only the test CAS are accepted. Spending goes to the `dev` budget (12 $ in all,
validated on 2026-10-16), kept in a separate database so that the app's own data stays untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from goulot.chain import service, store
from goulot.chain.identify import IdentificationError, identify
from goulot.chain.quote import reconstruction_quote
from goulot.db.connection import Database
from goulot.llm.backend import AnthropicBackend, RecordingBackend, ResumingBackend
from goulot.llm.budget import Budget
from goulot.llm.client import build_anthropic_client
from goulot.net.allowlist import load_allowlist
from goulot.net.client import build_client
from goulot.security.clock import SystemClock
from goulot.security.fs import ensure_private_dir
from goulot.security.logs import setup_logging
from goulot.security.secrets import get_secret
from goulot.settings import REPO_ROOT, Settings

TEST_CAS = ("107-13-1", "554-13-2", "7440-55-3")  # acrylonitrile, lithium carbonate, gallium
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "llm"
MAX_COST_PER_MATERIAL = 3.0  # phase 2 criterion


def summarize(chain: dict[str, Any]) -> dict[str, Any]:
    nodes = chain["nodes"]
    sites = [n for n in nodes if n["node_type"] in ("mine", "plant", "recycler", "port") and n["source_refs"]]
    return {
        "nodes": len(nodes),
        "edges": len(chain["edges"]),
        "routes": len(chain["routes"]),
        "countries": sorted({n["country_iso2"] for n in nodes if n["country_iso2"] != "unknown"}),
        "sourced_sites": len(sites),
        "confidence": dict(Counter(n["confidence"] for n in nodes)),
        "sources": len(chain["sources"]),
        "injection_suspected": chain["injection_suspected"],
    }


def record(settings: Settings, args: argparse.Namespace) -> None:
    cas = args.cas.strip()
    if cas not in TEST_CAS:
        sys.exit(f"Seuls les 3 CAS de test sont acceptés ({', '.join(TEST_CAS)}) : vos matières se reconstruisent "
                 "dans l'app, sans enregistrement dans le dépôt.")
    out = FIXTURES / cas
    if (out / "summary.json").is_file() and not args.force:
        sys.exit(f"Déjà enregistré dans {out.relative_to(REPO_ROOT)} : ajoutez --force pour recommencer.")
    resume = not args.force and (out / f"chain_research_{cas}.json").is_file()
    key = get_secret("anthropic")
    if not key:
        sys.exit("Clé API Anthropic absente : lancez « ./bin/goulot setup ».")
    setup_logging(settings.log_dir)
    budget, db = _dev_budget(settings)
    quote = reconstruction_quote()
    if resume:
        estimate, maximum = quote.structure_usd + quote.context_usd, quote.max_usd - quote.research_max_usd
        print(f"Reprise de {cas} : la recherche déjà payée est réutilisée, seule la structuration (2 appels) "
              "est relancée.")
        print(f"Devis : environ {estimate:.2f} $, {maximum:.2f} $ au plus.")
    else:
        maximum = quote.max_usd
        print(f"Enregistrement de {cas}. Devis : environ {quote.estimate_usd:.2f} $, {maximum:.2f} $ au plus.")
    print(f"Budget de test : {budget.spent('dev'):.2f} $ dépensés sur {budget.cap('dev'):.2f} $.")
    if budget.remaining("dev") < maximum:
        sys.exit("Budget de test insuffisant pour ce devis : rien n'est lancé.")
    if input("Lancer ? (o/N) ").strip().lower() != "o":
        sys.exit("Rien n'est lancé.")
    allowlist = load_allowlist(settings.config_dir)
    http = build_client(allowlist)
    try:
        try:
            identity = identify(http, cas)
        except IdentificationError as exc:
            sys.exit(str(exc))
        out.mkdir(parents=True, exist_ok=True)
        if not resume:
            for old in out.glob("*.json"):
                old.unlink()
        (out / "identity.json").write_text(json.dumps(identity.as_dict(), ensure_ascii=False, indent=1) + "\n",
                                           encoding="utf-8")
        with db.transaction() as conn:
            material_id = store.create_material(conn, identity.as_dict())
            store.clear_chain(conn, material_id)
            store.set_status(conn, material_id, "draft")
        backend = ResumingBackend(RecordingBackend(AnthropicBackend(build_anthropic_client(key, allowlist)), out), out)
        job_id = service.start_job(db, material_id)
        print("Reconstruction en cours (quelques minutes)…")
        cost = service.reconstruct(service.Deps(db, budget, backend, http, get_secret("comtrade")), material_id,
                                   job_id, budget_name="dev")
    finally:
        http.close()
    job = service.job_status(db, job_id) or {}
    print(f"\nRésultat : {job.get('message', '')}")
    print(f"Coût réel de cette matière : {cost:.2f} $ (critère : moins de {MAX_COST_PER_MATERIAL:.0f} $).")
    print(f"Budget de test : {budget.spent('dev'):.2f} $ dépensés sur {budget.cap('dev'):.2f} $.")
    if job.get("state") != "done":
        sys.exit("Échec. Les réponses déjà payées sont gardées : la même commande, sans --force, reprend là où "
                 "ça s'est arrêté. Copiez ce message à Claude.")
    with db.connect() as conn:
        summary = summarize(store.load_chain(conn, material_id))
        total = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE material_id = ?",
                             (material_id,)).fetchone()[0]
    summary["cost_usd"] = round(float(total), 4)  # every run for this material, failed ones included
    print(f"Coût total de cette matière, essais ratés compris : {float(total):.2f} $.")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Maillons : {summary['nodes']}, voies de production : {summary['routes']}, "
          f"pays : {len(summary['countries'])}, sites sourcés : {summary['sourced_sites']}, "
          f"sources : {summary['sources']}.")
    print(f"Enregistré dans {Path(out).relative_to(REPO_ROOT)}. Dites à Claude que c'est fait.")


CASES_DIR = "cases"
MIN_CASES = 7  # phase 3 criterion was 10 per test CAS; lowered on 2026-10-19 with the user (gallium: 7 events)


def cases_recorded(cas: str) -> bool:
    return (FIXTURES / cas / CASES_DIR / "summary.json").is_file()


def record_cases(settings: Settings, args: argparse.Namespace) -> None:
    """Real case preparation for one test CAS (its recorded chain), answers in tests/fixtures/llm/<CAS>/cases/."""
    from datetime import date

    from goulot.history import leads, library

    cas = args.cas.strip()
    if cas not in TEST_CAS:
        sys.exit(f"Seuls les 3 CAS de test sont acceptés ({', '.join(TEST_CAS)}) : les cas de vos matières se "
                 "préparent dans l'app, sans enregistrement dans le dépôt.")
    out = FIXTURES / cas / CASES_DIR
    if cases_recorded(cas) and not args.force:
        sys.exit(f"Déjà enregistré dans {out.relative_to(REPO_ROOT)} : ajoutez --force pour recommencer.")
    key = get_secret("anthropic")
    if not key:
        sys.exit("Clé API Anthropic absente : lancez « ./bin/goulot setup ».")
    setup_logging(settings.log_dir)
    budget, db = _dev_budget(settings)
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM materials WHERE cas = ?", (cas,)).fetchone()
        has_chain = row is not None and conn.execute(
            "SELECT 1 FROM chain_nodes WHERE material_id = ? LIMIT 1", (row["id"],)).fetchone() is not None
    if not has_chain:
        sys.exit(f"La chaîne de test {cas} n'est pas dans la base de test : « ./bin/goulot dev record --cas {cas} ».")
    quote = library.cases_quote()
    print(f"Préparation des cas comparables de {cas} : recherche web (10 recherches au plus) puis structuration.")
    print(f"Devis : environ {quote.estimate_usd:.2f} $, {quote.max_usd:.2f} $ au plus "
          f"(critère : moins de {MAX_COST_PER_MATERIAL:.0f} $).")
    print(f"Budget de test : {budget.spent('dev'):.2f} $ dépensés sur {budget.cap('dev'):.2f} $.")
    if budget.remaining("dev") < quote.max_usd:
        sys.exit("Budget de test insuffisant pour ce devis : rien n'est lancé.")
    if input("Lancer ? (o/N) ").strip().lower() != "o":
        sys.exit("Rien n'est lancé.")
    out.mkdir(parents=True, exist_ok=True)
    if args.force:
        for old in out.glob("*.json"):
            old.unlink()
    today = date.today()
    allowlist = load_allowlist(settings.config_dir)
    http = build_client(allowlist)

    def keep(found: list[leads.Lead]) -> None:
        (out / "leads.json").write_text(json.dumps([lead.as_dict() for lead in found], ensure_ascii=False,
                                                   indent=1) + "\n", encoding="utf-8")

    try:
        with db.transaction() as conn:
            conn.execute("DELETE FROM case_sources WHERE case_row IN (SELECT id FROM historical_events WHERE "
                         "material_id = ?)", (row["id"],))
            conn.execute("DELETE FROM historical_events WHERE material_id = ?", (row["id"],))
        recorded = None
        if (out / "leads.json").is_file():  # resume: the same leads as the paid research
            recorded = [leads.Lead.from_dict(d) for d in json.loads((out / "leads.json").read_text("utf-8"))]
        backend = ResumingBackend(RecordingBackend(AnthropicBackend(build_anthropic_client(key, allowlist)), out), out)
        job_id = library.start_job(db, row["id"])
        print("Préparation en cours (quelques minutes)…")
        cost = library.prepare(library.CaseDeps(db, budget, backend, http, recorded, keep), row["id"], job_id,
                               budget_name="dev", today=today)
    finally:
        http.close()
    with db.connect() as conn:
        job = conn.execute("SELECT state, message FROM jobs WHERE id = ?", (job_id,)).fetchone()
        found = library.cases(conn, row["id"])
    print(f"\nRésultat : {job['message']}")
    print(f"Coût réel : {cost:.2f} $. Budget de test : {budget.spent('dev'):.2f} $ sur {budget.cap('dev'):.2f} $.")
    if job["state"] != "done":
        sys.exit("Échec. Les réponses déjà payées sont gardées : la même commande, sans --force, reprend là où "
                 "ça s'est arrêté. Copiez ce message à Claude.")
    summary = {"today": today.isoformat(), "cases": len(found), "cost_usd": round(cost, 4),
               "with_anomalies": sum(1 for c in found if c["anomalies"]),
               "case_ids": [c["case_id"] for c in found]}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Cas préparés : {len(found)} (au moins {MIN_CASES} attendus), dont {summary['with_anomalies']} avec une "
          "remarque du contrôle.")
    print(f"Enregistré dans {out.relative_to(REPO_ROOT)}. Copiez ce résultat à Claude.")


def _dev_budget(settings: Settings) -> tuple[Budget, Database]:
    dev_home = settings.home / "dev"
    ensure_private_dir(dev_home)
    db = Database(dev_home / "record.sqlite")
    db.migrate()
    return Budget(db, SystemClock()), db


PROBE_REPORT = (
    "## 1. Voies de production\nL'ammoxydation du propylène domine [src1].\n"
    "## 3. Pays\nLa Chine produit environ 40 % [src2]. Usine fictive de test à Anytown [src2]."
)
PROBE_SOURCES = [
    {"ref": "src1", "url": "https://example.org/a", "title": "Rapport test", "publisher": "example.org",
     "page_age": ""},
    {"ref": "src2", "url": "https://example.org/b", "title": "Statistique test", "publisher": "example.org",
     "page_age": ""},
]
PROBE_MAX_USD = 0.15


def _probe_ladder() -> list[tuple[str, str, dict[str, Any]]]:
    """(name, meaning, request) for every output schema the app sends, plus fallbacks for the analysis."""
    from goulot.chain.structure import context_params, structure_params
    from goulot.cli.schema_probe import enums_to_descriptions, only
    from goulot.llm.prompts import load_prompt
    from goulot.llm.validate import schema

    def req(model: str, prompt: str, out_schema: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"model": model, "max_tokens": 64, "system": load_prompt(prompt), **extra,
                "output_config": {**extra.pop("output_config", {}), "format": {"type": "json_schema",
                                                                               "schema": out_schema}},
                "messages": [{"role": "user", "content": "Essai technique : réponds par un objet minimal."}]}

    material = {"name": "acrylonitrile", "cas": TEST_CAS[0], "formula": "C3H3N", "purchased_form": "acrylonitrile"}
    nodes = [{"node_id": "acrylonitrile", "node_type": "material", "name": "Acrylonitrile"}]
    impact = schema("impact_assessment_v1")
    sonnet_high = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
    return [
        ("structure", "chaîne : maillons et liens", {**structure_params(material, PROBE_REPORT, PROBE_SOURCES),
                                                     "max_tokens": 64}),
        ("contexte", "chaîne : voies, risques, échéances",
         {**context_params(material, PROBE_REPORT, PROBE_SOURCES, nodes), "max_tokens": 64}),
        ("tri", "tri des articles (Haiku)", req("claude-haiku-5-5", "triage_v1", schema("triage_v1"),
                                                thinking={"type": "disabled"})),
        ("cas", "cas comparables : structuration",
         req("claude-sonnet-5-5", "case_structure_v1", schema("historical_case_v1"),
             thinking={"type": "adaptive"}, output_config={"effort": "medium"})),
        ("synthese", "synthèse hebdomadaire", req("claude-sonnet-5-5", "digest_v1", schema("digest_v1"),
                                                   thinking={"type": "between_tools"},
                                                   output_config={"effort": "medium"})),
        # The three partial calls of the impact analysis, exactly as the app sends them (prompt and schema)...
        ("analyse_comprehension", "analyse 1/3 : compréhension et conclusion",
         req("claude-sonnet-5-5", "impact_v1", schema("impact_part_comprehension_v1"), **sonnet_high)),
        ("analyse_impacts", "analyse 2/3 : impacts et signaux à suivre",
         req("claude-sonnet-5-5", "impact_v1", schema("impact_part_impacts_v1"), **sonnet_high)),
        ("analyse_hypotheses", "analyse 3/3 : hypothèses de prix",
         req("claude-sonnet-5-5", "impact_v1", schema("impact_part_hypotheses_v1"), **sonnet_high)),
        # ...and two narrower fallbacks, in case the impacts part is still too large.
        ("impacts_seuls", "repli : impacts sans signaux à suivre",
         req("claude-sonnet-5-5", "impact_v1", only(impact, "event_id", "commodity_impacts"), **sonnet_high)),
        ("impacts_sans_enum", "repli : impacts seuls, listes en description",
         req("claude-sonnet-5-5", "impact_v1", enums_to_descriptions(only(impact, "event_id", "commodity_impacts")),
             **sonnet_high)),
    ]


def probe(settings: Settings, only: str | None = None) -> None:
    """Sends each request of the ladder with a 64-token cap: refusals are free, acceptances cost < 1 cent."""
    import anthropic

    from goulot.llm.budget import CallRecord
    from goulot.llm.pricing import Usage, cost_usd

    key = get_secret("anthropic")
    if not key:
        sys.exit("Clé API Anthropic absente : lancez « ./bin/goulot setup ».")
    setup_logging(settings.log_dir)
    budget, _ = _dev_budget(settings)
    ladder = _probe_ladder()
    if only:
        wanted = {name.strip() for name in only.split(",")}
        unknown = wanted - {name for name, _, _ in ladder}
        if unknown:
            sys.exit(f"Schémas inconnus : {', '.join(sorted(unknown))}.")
        ladder = [step for step in ladder if step[0] in wanted]
    print(f"Essai à blanc de {len(ladder)} schémas de sortie, réponses limitées à 64 tokens.")
    print(f"Un schéma refusé ne coûte rien ; un schéma accepté, moins d'un centime ({PROBE_MAX_USD:.2f} $ au plus).")
    print(f"Budget de test : {budget.spent('dev'):.2f} $ dépensés sur {budget.cap('dev'):.2f} $.")
    budget.check("dev", PROBE_MAX_USD)
    if input("Lancer ? (o/N) ").strip().lower() != "o":
        sys.exit("Rien n'est lancé.")
    backend = AnthropicBackend(build_anthropic_client(key, load_allowlist(settings.config_dir)))
    total = 0.0
    for name, meaning, params in ladder:
        started = time.monotonic()
        try:
            response = backend.create(f"probe_{name}", params)
        except anthropic.BadRequestError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            message = str((body.get("error") or {}).get("message", ""))[:90]
            print(f"  ❌ {name} ({meaning}) : refusé, {message}")
            continue
        except anthropic.APIError as exc:
            sys.exit(f"Arrêt : erreur {type(exc).__name__} sur {name}. Copiez ce message à Claude.")
        usage = Usage.from_api(response.get("usage"))
        cost = cost_usd(str(params["model"]), usage)
        total += cost
        budget.record(CallRecord("dev", "schema_probe", str(params["model"]), "standard", usage, cost,
                                 request_id=str(response.get("_request_id", "")),
                                 stop_reason=str(response.get("stop_reason", ""))))
        print(f"  ✅ {name} ({meaning}) : accepté en {time.monotonic() - started:.0f} s, {cost:.4f} $")
    print(f"Coût de l'essai : {total:.3f} $. Budget de test : {budget.spent('dev'):.2f} $ "
          f"sur {budget.cap('dev'):.2f} $.")
    print("Copiez tout ce résultat à Claude.")


WATCH_MAX_USD = 1.0  # the phase 3 test budget is 3 $ in all; one scenario run stays well under 1 $


def record_watch(settings: Settings, args: argparse.Namespace) -> None:
    """Real triage and analysis (Batch) of the fixed scenario, answers recorded for the replay tests."""
    from goulot.cli import watch_scenario
    from goulot.llm.batch import AnthropicBatchBackend, RecordingBatchBackend, ResumingBatchBackend
    from goulot.llm.budget import CallRecord
    from goulot.llm.pricing import Usage
    from goulot.vault.keys import Vault

    key = get_secret("anthropic")
    if not key:
        sys.exit("Clé API Anthropic absente : lancez « ./bin/goulot setup ».")
    if not (FIXTURES / watch_scenario.CAS / "summary.json").is_file():
        sys.exit("La chaîne de test 107-13-1 n'est pas enregistrée : « ./bin/goulot dev record --cas 107-13-1 ».")
    if watch_scenario.recorded() and not args.force:
        sys.exit("Scénario déjà enregistré : ajoutez --force pour recommencer.")
    setup_logging(settings.log_dir)
    budget, _ = _dev_budget(settings)
    print("Enregistrement du scénario de veille (articles fictifs sur l'acrylonitrile) : tri par Haiku 5.5 et analyse")
    print("par Sonnet 5.5 en 3 appels partiels, en traitement différé (moitié prix).")
    print(f"Devis : environ 0,10 à 0,30 $, {WATCH_MAX_USD:.2f} $ au plus.")
    print(f"Budget de test : {budget.spent('dev'):.2f} $ dépensés sur {budget.cap('dev'):.2f} $.")
    budget.check("dev", WATCH_MAX_USD)
    if input("Lancer ? (o/N) ").strip().lower() != "o":
        sys.exit("Rien n'est lancé.")
    run_home = settings.home / "dev" / "watch"
    if args.force and run_home.exists():
        import shutil

        shutil.rmtree(run_home)
    if args.force:  # triage requests do not change: their answers are reused (and not paid again)
        for old in [*watch_scenario.RECORDINGS.glob("ana_*.json"), watch_scenario.RECORDINGS / "summary.json"]:
            old.unlink(missing_ok=True)
    ensure_private_dir(run_home)
    db = Database(run_home / "scenario.sqlite")
    db.migrate()
    with db.transaction() as conn:
        conn.execute("DELETE FROM llm_calls")  # the scenario's own copy; the test budget lives in record.sqlite
    live = AnthropicBatchBackend(build_anthropic_client(key, load_allowlist(settings.config_dir)))
    backend = ResumingBatchBackend(RecordingBatchBackend(live, watch_scenario.RECORDINGS), watch_scenario.RECORDINGS)
    started = time.monotonic()

    def poll() -> None:
        print(f"  … traitement différé en cours ({time.monotonic() - started:.0f} s)", flush=True)
        time.sleep(20)
        if time.monotonic() - started > 3600:
            sys.exit("Plus d'une heure d'attente : relancez la même commande plus tard, rien ne sera repayé.")

    scenario_budget = Budget(db, SystemClock())
    result = watch_scenario.run(db, scenario_budget, Vault(db), backend, poll=poll)
    spent = float(result["ai_cost_usd"])  # triage and analysis only: the chain is replayed, already paid
    budget.record(CallRecord("dev", "watch_scenario", "claude-sonnet-5-5", "batch", Usage(), spent))
    watch_scenario.save_summary(result)
    print(f"\nÉvénements : {len(result['events'])} ; verdict : {result['verdict']}.")
    print(f"Coût réel : {spent:.3f} $. Budget de test : {budget.spent('dev'):.2f} $ sur {budget.cap('dev'):.2f} $.")
    print("Enregistré dans tests/fixtures/llm/watch_107-13-1. Copiez ce résultat à Claude.")


def fetch_cities(settings: Settings) -> None:
    """Download the GeoNames list of cities above 15,000 inhabitants through the app's client, keep its reduced form
    in `goulot/data/cities.json.gz` (offline place lookup of Ma chaîne). Public data, no key, no AI."""
    from goulot.net.client import fetch
    from goulot.private import cities

    host = urlsplit(cities.SOURCE_URL).hostname or ""
    print(f"Téléchargement de la liste des villes GeoNames ({host}), sans IA ni clé…")
    client = build_client(load_allowlist(settings.config_dir, [host]))  # this host for this command only
    try:
        result = fetch(client, cities.SOURCE_URL, max_bytes=20 * 1024 * 1024)
    finally:
        client.close()
    if result.status != 200:
        sys.exit(f"Réponse inattendue de GeoNames : {result.status}.")
    rows = cities.reduce_dump(result.content)
    size = cities.write(rows)
    digest = hashlib.sha256(result.content).hexdigest()
    print(f"{len(rows)} villes gardées dans goulot/data/cities.json.gz ({size / 1e6:.1f} Mo, compressé).")
    print(f"Archive téléchargée : {len(result.content) / 1e6:.1f} Mo, empreinte SHA-256 {digest[:16]}….")
    print("Terminé. Copiez ce résultat à Claude.")


def watch_report(settings: Settings, days: int, live: bool) -> None:
    """Read-only: the base is opened without migrating it, so the installed app's base is never changed."""
    import sqlite3
    from datetime import UTC, datetime

    from goulot.collect import report
    from goulot.net.allowlist import load_allowlist
    from goulot.net.client import build_client
    from goulot.security.secrets import get_secret

    if not settings.db_path.exists():
        sys.exit("Base absente : Goulot n'est pas installé sur ce Mac.")
    conn = sqlite3.connect(f"{settings.db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        now = datetime.now(UTC)
        found = report.history(conn, now, max(1, min(days, 90)))
        sample = None
        if live:
            print("Essai à blanc des sources (une à deux minutes, sans IA, rien n'est enregistré)…")
            client = build_client(load_allowlist(settings.config_dir))
            try:
                sample = report.live(conn, client, get_secret, now)
            finally:
                client.close()
        print(report.render(found, sample))
    finally:
        conn.close()
    print("Terminé. Copiez ce résultat à Claude.")


def cmd_dev(settings: Settings, args: argparse.Namespace) -> None:
    if args.dev_action == "record":
        record(settings, args)
    elif args.dev_action == "probe":
        probe(settings, getattr(args, "only", None))
    elif args.dev_action == "record-watch":
        record_watch(settings, args)
    elif args.dev_action == "record-cases":
        record_cases(settings, args)
    elif args.dev_action == "check-sources":
        from goulot.cli import check_sources

        print("Interrogation des sources depuis votre Mac (sans IA, sans clé) ; réponses gardées dans .samples/.")
        check_sources.run(settings)
        print("Terminé. Copiez ce résultat à Claude.")
    elif args.dev_action == "fetch-cities":
        fetch_cities(settings)
    elif args.dev_action == "watch-report":
        watch_report(settings, args.days, args.live)
