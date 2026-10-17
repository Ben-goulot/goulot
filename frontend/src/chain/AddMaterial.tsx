import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { clip, safeHref, type Identity, type Job, type Material, type Quote } from "./types";

const STEPS = ["Identification", "Recherche sourcée", "Structuration", "Recoupement", "Délais"];
const JOB_POLL_MS = 3_000; // only while a reconstruction runs

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Erreur.";
}

const usd = (v: number) => `${v.toFixed(2).replace(".", ",")} $`;

export function AddMaterial(props: { onOpenChain: (id: string) => void; resume?: Material }) {
  const [query, setQuery] = useState("");
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [material, setMaterial] = useState<Material | null>(props.resume ?? null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!job || job.state !== "running") return;
    const id = window.setInterval(async () => {
      try {
        setJob(await api.get<Job>(`/api/jobs/${job.id}`));
      } catch (e) {
        setError(errorText(e));
      }
    }, JOB_POLL_MS);
    return () => window.clearInterval(id);
  }, [job]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const search = () =>
    run(async () => {
      setIdentity(await api.post<Identity>("/api/materials/identify", { query }));
    });

  const askQuote = () =>
    run(async () => {
      const m = material ?? (await api.post<Material>("/api/materials", { query: identity?.cas ?? query }));
      setMaterial(m);
      setQuote(await api.post<Quote>(`/api/materials/${m.id}/quote`));
    });

  const launch = () =>
    run(async () => {
      if (!material || !quote) return;
      const { job_id } = await api.post<{ job_id: string }>(`/api/materials/${material.id}/reconstruct`, {
        quote_id: quote.quote_id,
      });
      setQuote(null);
      setJob({ id: job_id, step: 1, step_label: STEPS[0], state: "running", message: "", cost_usd: 0 });
    });

  const shown = identity ?? (material ? { ...material, name_fr: "", sources: material.identity_sources } : null);

  return (
    <section className="panel">
      <h2>Ajouter une matière</h2>
      {!material && (
        <div className="row">
          <input
            value={query}
            maxLength={100}
            placeholder="Nom ou numéro CAS"
            aria-label="Nom ou numéro CAS"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && query.trim() && void search()}
          />
          <button className="primary" disabled={busy || !query.trim()} onClick={search}>
            Rechercher
          </button>
        </div>
      )}
      {error && <p className="warn">{error}</p>}

      {shown && (
        <div className="card">
          <p className="eyebrow">Identifiée</p>
          <h3 className="title">{clip(shown.name_fr || shown.name, 120)}</h3>
          <dl className="facts">
            <dt>Numéro CAS</dt>
            <dd className="mono">{shown.cas}</dd>
            {shown.formula && (
              <>
                <dt>Formule</dt>
                <dd className="mono">{shown.formula}</dd>
              </>
            )}
            {shown.synonyms.length > 0 && (
              <>
                <dt>Synonymes</dt>
                <dd>{clip(shown.synonyms.slice(0, 6).join(", "), 200)}</dd>
              </>
            )}
            <dt>Code douanier</dt>
            <dd>proposé à la reconstruction, marqué « à vérifier »</dd>
            <dt>Sources</dt>
            <dd>
              {shown.sources.map((s) =>
                safeHref(s.url) ? (
                  <a key={s.url} href={safeHref(s.url)} target="_blank" rel="noopener noreferrer" className="source">
                    {s.name}
                  </a>
                ) : null,
              )}
            </dd>
          </dl>
          {!quote && !job && (
            <div className="row">
              <button className="primary" disabled={busy} onClick={askQuote}>
                Demander le devis de reconstruction
              </button>
              {!material && <button onClick={() => setIdentity(null)}>Ce n'est pas la bonne substance</button>}
            </div>
          )}
        </div>
      )}

      {quote && (
        <div className="card">
          <p className="eyebrow">Devis</p>
          <p>
            Environ <strong className="mono">{usd(quote.estimate_usd)}</strong>, <span className="mono">{usd(quote.max_usd)}</span> au
            plus ({quote.searches_max} recherches web au maximum). Quelques minutes.
          </p>
          <p className="muted">
            Plafond des reconstructions ce mois-ci : {usd(quote.remaining_usd)} restants sur {usd(quote.cap_usd)}.
          </p>
          <div className="row">
            <button className="primary" disabled={busy || quote.remaining_usd < quote.max_usd} onClick={launch}>
              Confirmer et lancer
            </button>
            <button onClick={() => setQuote(null)}>Annuler</button>
          </div>
        </div>
      )}

      {job && (
        <div className="card">
          <p className="eyebrow">Reconstruction de la chaîne</p>
          <ol className="steps">
            {STEPS.map((label, i) => {
              const n = i + 1;
              const state =
                job.state === "done" || n < job.step ? "fait" : n === job.step && job.state === "running" ? "en cours" : n === job.step ? "arrêté" : "ensuite";
              return (
                <li key={label} className={`step ${state.replace(" ", "-")}`}>
                  {label} <span className="muted">· {state}</span>
                </li>
              );
            })}
            <li className={`step ${job.state === "done" ? "en-cours" : "ensuite"}`}>
              Tu relis la chaîne et corriges les maillons douteux{" "}
              <span className="muted">· {job.state === "done" ? "à toi de jouer" : "ensuite"}</span>
            </li>
          </ol>
          {job.state !== "running" && (
            <p className={job.state === "failed" ? "warn" : ""}>
              {job.message} Coût réel : <span className="mono">{usd(job.cost_usd)}</span>.
            </p>
          )}
          {job.state === "done" && material && (
            <button className="primary" onClick={() => props.onOpenChain(material.id)}>
              Relire et valider la chaîne
            </button>
          )}
          {job.state === "failed" && (
            <button onClick={() => { setJob(null); void askQuote(); }}>Nouveau devis</button>
          )}
        </div>
      )}
    </section>
  );
}
