"""`goulot dev record --cas <CAS>`: real reconstruction of one of the 3 test CAS, AI answers recorded in
`tests/fixtures/llm/<CAS>/` for the offline tests (CLAUDE.md, Budget). Run by the user in their own Terminal.

Never for the user's own CAS: only the test CAS are accepted. Spending goes to the `dev` budget (12 $ in all,
validated on 2026-10-16), kept in a separate database so that the app's own data stays untouched.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

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


def probe(settings: Settings) -> None:
    """Sends the structuring request with each schema variant and a 64-token cap: refusals are free."""
    import anthropic

    from goulot.chain.structure import context_params, structure_params
    from goulot.llm.budget import CallRecord
    from goulot.llm.pricing import Usage, cost_usd

    key = get_secret("anthropic")
    if not key:
        sys.exit("Clé API Anthropic absente : lancez « ./bin/goulot setup ».")
    setup_logging(settings.log_dir)
    budget, _ = _dev_budget(settings)
    print("Essai à blanc des 2 appels de structuration (schémas réels), réponses limitées à 64 tokens.")
    print(f"Un schéma refusé ne coûte rien ; un schéma accepté, moins d'un centime ({PROBE_MAX_USD:.2f} $ au plus).")
    print(f"Budget de test : {budget.spent('dev'):.2f} $ dépensés sur {budget.cap('dev'):.2f} $.")
    budget.check("dev", PROBE_MAX_USD)
    if input("Lancer ? (o/N) ").strip().lower() != "o":
        sys.exit("Rien n'est lancé.")
    backend = AnthropicBackend(build_anthropic_client(key, load_allowlist(settings.config_dir)))
    material = {"name": "acrylonitrile", "cas": TEST_CAS[0], "formula": "C3H3N", "purchased_form": "acrylonitrile"}
    nodes = [{"node_id": "acrylonitrile", "node_type": "material", "name": "Acrylonitrile"},
             {"node_id": "cn_acrylonitrile", "node_type": "country", "name": "Chine"}]
    ladder = [
        ("structure", "maillons et liens", structure_params(material, PROBE_REPORT, PROBE_SOURCES)),
        ("contexte", "voies, substituts, risques, échéances",
         context_params(material, PROBE_REPORT, PROBE_SOURCES, nodes)),
    ]
    total = 0.0
    for name, meaning, base in ladder:
        params = {**base, "max_tokens": 64}
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


def cmd_dev(settings: Settings, args: argparse.Namespace) -> None:
    if args.dev_action == "record":
        record(settings, args)
    elif args.dev_action == "probe":
        probe(settings)
