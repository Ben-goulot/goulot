import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { clip, safeHref } from "../chain/types";
import { CONFIDENCE_LABEL, EVENT_TYPE_LABEL, signed, type CaseLibrary as Data, type PastCase } from "./types";

// « Bibliothèque de cas » (docs/SPEC.md, section 6): cases prepared by the AI arrive as drafts; the user checks
// each one against its sources, then verifies or rejects it. Only verified cases count in a median or a dossier.

const STATUS = { draft: "Brouillon IA à vérifier", verified: "Vérifié", rejected: "Écarté" };
const DIRECTION = { tightening: "resserrement de l'offre", loosening: "desserrement", ambiguous: "effet ambigu" };
const JOB_STEP: Record<number, string> = { 1: "Pistes des bases publiques", 2: "Recherche web", 3: "Structuration" };
const JOB_POLL_MS = 3000;

type Quote = { quote_id: string; estimate_usd: number; max_usd: number; searches_max: number; remaining_usd: number };
type Job = { id: string; state: string; step: number; message: string; cost_usd: number };

export function CaseLibrary(props: { materialId: string; onBack: () => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [filter, setFilter] = useState<"draft" | "verified" | "rejected" | "all">("draft");

  const load = useCallback(async () => {
    try {
      const d = await api.get<Data>(`/api/materials/${props.materialId}/cases`);
      setData(d);
      if (d.job?.state === "running") setJob(d.job);
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  }, [props.materialId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!job || job.state !== "running") return;
    const id = window.setInterval(async () => {
      try {
        const next = await api.get<Job>(`/api/jobs/${job.id}`);
        setJob(next);
        if (next.state !== "running") {
          setMessage(next.message);
          await load();
        }
      } catch (e) {
        setMessage(e instanceof ApiError ? e.message : "Erreur.");
      }
    }, JOB_POLL_MS);
    return () => window.clearInterval(id);
  }, [job, load]);

  const act = async (fn: () => Promise<void>) => {
    setMessage(null);
    try {
      await fn();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };

  if (!data) return <section className="panel">{message ?? "Chargement…"}</section>;
  const shown = data.cases.filter((c) => filter === "all" || c.status === filter);
  const count = (s: string) => data.cases.filter((c) => c.status === s).length;

  return (
    <div className="sheet">
      <header className="sheet-head">
        <button className="tab" onClick={props.onBack}>
          ← Fiche matière
        </button>
        <h2>Bibliothèque de cas · {clip(data.material.name, 60)}</h2>
        <span className="mono muted">CAS {data.material.cas}</span>
      </header>
      {message && <p className="banner">{message}</p>}

      <section className="panel">
        <p className="eyebrow">Préparer des cas passés</p>
        <p className="muted">
          L'IA cherche les événements passés qui ont touché la matière ou ses intrants directs, sources à l'appui ; le
          code mesure ensuite, sur les séries de prix, ce qui s'est passé après. Les cas arrivent en brouillon.
        </p>
        {job?.state === "running" ? (
          <p>
            En cours : {JOB_STEP[job.step] ?? "préparation"}… <span className="muted">({job.cost_usd.toFixed(2)} $ dépensés)</span>
          </p>
        ) : quote ? (
          <div className="row">
            <span>
              Devis : environ {quote.estimate_usd.toFixed(2)} $, {quote.max_usd.toFixed(2)} $ au plus ({quote.searches_max} recherches web),
              sur le plafond des reconstructions ({quote.remaining_usd.toFixed(2)} $ restants).
            </span>
            <button
              className="primary"
              onClick={() =>
                act(async () => {
                  const r = await api.post<{ job_id: string }>(`/api/materials/${props.materialId}/cases/prepare`, { quote_id: quote.quote_id });
                  setQuote(null);
                  setJob({ id: r.job_id, state: "running", step: 1, message: "", cost_usd: 0 });
                })
              }
            >
              Confirmer et lancer
            </button>
            <button onClick={() => setQuote(null)}>Annuler</button>
          </div>
        ) : (
          <button onClick={() => act(async () => setQuote(await api.post<Quote>(`/api/materials/${props.materialId}/cases/quote`)))}>
            Demander un devis
          </button>
        )}
      </section>

      {data.summaries.length > 0 && (
        <section className="panel">
          <p className="eyebrow">Réactions mesurées (cas vérifiés)</p>
          <ul className="plain">
            {data.summaries.map((s) => (
              <li key={s.instrument_id}>
                {clip(s.name, 80)} :{" "}
                {s.n === 0 ? (
                  <span className="muted">aucun cas vérifié mesuré</span>
                ) : (
                  <span className="mono">
                    médiane {signed(s.median)} % à {s.horizon} {s.horizon === 20 ? "séances" : "pas"}, de {signed(s.low)} à {signed(s.high)} % ({s.n} cas :{" "}
                    {s.years.join(", ")})
                  </span>
                )}
                {s.excluded_overlap > 0 && <span className="muted"> · {s.excluded_overlap} écarté(s) pour chevauchement</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="panel">
        <div className="row">
          {(["draft", "verified", "rejected", "all"] as const).map((f) => (
            <button key={f} className={filter === f ? "tab active" : "tab"} onClick={() => setFilter(f)}>
              {f === "all" ? `Tous (${data.cases.length})` : `${STATUS[f]} (${count(f)})`}
            </button>
          ))}
        </div>
        {shown.length === 0 ? (
          <p className="muted">Aucun cas ici.</p>
        ) : (
          <ul className="plain cases">
            {shown.map((c) => (
              <CaseItem key={c.id} c={c} onReview={(action) => act(async () => {
                await api.post(`/api/materials/${props.materialId}/cases/${c.id}/review`, { action });
                await load();
              })} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function CaseItem({ c, onReview }: { c: PastCase; onReview: (action: "verify" | "reject" | "reopen") => void }) {
  const r = c.reactions[0];
  const blocked = c.anomalies.some((a) => a.includes("non vérifiable"));
  return (
    <li className={`case ${c.status}`}>
      <div className="row spread">
        <strong>
          <span className="mono">{c.first_report_date}</span> {clip(c.title, 160)}
        </strong>
        <span className="badge neutral">{STATUS[c.status]}</span>
      </div>
      <p className="muted">
        {clip(c.place, 80)} · {EVENT_TYPE_LABEL[c.event_type] ?? c.event_type} · {DIRECTION[c.direction]} · part de capacité :{" "}
        {c.capacity_share_pct === null ? "inconnue" : `${c.capacity_share_pct.toFixed(1).replace(".", ",")} % (${clip(c.capacity_share_basis, 80)})`}
        {c.duration_days !== null ? ` · ${c.duration_days.toFixed(0)} jours` : ""} · confiance {CONFIDENCE_LABEL[c.confidence] ?? c.confidence}
        {c.overlap ? " · chevauche un autre cas" : ""}
      </p>
      <p>{clip(c.summary, 600)}</p>
      {r && (
        <p className="mono small">
          {clip(r.name, 60)} — variation propre : {Object.entries(r.own).map(([h, v]) => `${h} : ${signed(v)} %`).join(" · ")}
          {r.notes.length > 0 && <span className="muted"> ({r.notes.join(" ")})</span>}
        </p>
      )}
      {c.anomalies.length > 0 && <p className="warn">{c.anomalies.join(" ")}</p>}
      <ul className="plain inline">
        {c.sources.map((s) => (
          <li key={s.ref}>
            <a href={safeHref(s.url)} target="_blank" rel="noopener noreferrer" className="source">
              {clip(s.title || s.publisher, 80)}
            </a>{" "}
            <span className="muted small">
              {s.publisher} · fiabilité {s.reliability.toFixed(1)}
            </span>
          </li>
        ))}
      </ul>
      <div className="row">
        {c.status !== "verified" && (
          <button className="primary" disabled={blocked} onClick={() => onReview("verify")}>
            Vérifié, sources lues
          </button>
        )}
        {c.status !== "rejected" && <button onClick={() => onReview("reject")}>Écarter</button>}
        {c.status !== "draft" && <button onClick={() => onReview("reopen")}>Remettre en brouillon</button>}
      </div>
    </li>
  );
}
