import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { SourceLinks } from "./SourceLinks";
import { CHANGE_TYPE, clip, CONFIDENCE, shareText, STATUS, type Chain, type Job, type ShareBasis, type SiteStatus, type Source, type UpdateItem, type UpdateQuote } from "./types";

const JOB_POLL_MS = 3_000; // only while an update runs
const usd = (v: number) => `${v.toFixed(2).replace(".", ",")} $`;

function frDate(iso: string | null): string {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-");
  return d ? `${d}/${m}/${y}` : iso;
}

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Erreur.";
}

/** What an accepted proposal will write, in plain words. Model texts are shown as text only. */
function detail(item: UpdateItem): string {
  const p = item.payload;
  switch (item.change_type) {
    case "status":
      return `Nouvel état : ${STATUS[p.status as SiteStatus] ?? String(p.status)}.`;
    case "share":
      return `Nouvelle part : ${shareText({ global_share_pct: Number(p.global_share_pct), share_basis: p.share_basis as ShareBasis, share_year: p.share_year ? Number(p.share_year) : null })}.`;
    case "capacity":
      return `Nouvelle capacité : ${clip(String(p.capacity_text), 120)}.`;
    case "new_producer":
      return `${clip(String(p.name), 120)} (${String(p.country_iso2)}), ${STATUS[p.status as SiteStatus] ?? "état non précisé"}, produit : ${clip(String(p.product), 80)}.`;
    case "date":
      return `${String(p.date)} : ${clip(String(p.what), 200)}`;
    default:
      return "";
  }
}

export function UpdatePanel(props: { materialId: string; chain: Chain; sources: Map<string, Source>; editable: boolean; onChanged: () => Promise<void> }) {
  const { freshness: f, items, last_update: last } = props.chain.update;
  const [quote, setQuote] = useState<UpdateQuote | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const names = new Map(props.chain.nodes.map((n) => [n.node_key, n.name]));

  useEffect(() => {
    if (!job || job.state !== "running") return;
    const id = window.setInterval(async () => {
      try {
        const next = await api.get<Job>(`/api/jobs/${job.id}`);
        setJob(next);
        if (next.state !== "running") await props.onChanged();
      } catch (e) {
        setError(errorText(e));
      }
    }, JOB_POLL_MS);
    return () => window.clearInterval(id);
  }, [job, props]);

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

  const ask = (kind: "search" | "reread") =>
    run(async () => {
      setJob(null);
      setQuote(await api.post<UpdateQuote>(`/api/materials/${props.materialId}/update/quote`, { kind }));
    });

  const launch = () =>
    run(async () => {
      if (!quote) return;
      const { job_id } = await api.post<{ job_id: string }>(`/api/materials/${props.materialId}/update`, {
        kind: quote.kind,
        quote_id: quote.quote_id,
      });
      setQuote(null);
      setJob({ id: job_id, step: 1, step_label: quote.kind === "search" ? "Recherche des changements" : "Propositions", state: "running", message: "", cost_usd: 0 });
    });

  const decide = (item: UpdateItem, accept: boolean) =>
    run(async () => {
      await api.post(`/api/materials/${props.materialId}/updates/${item.id}`, { accept });
      await props.onChanged();
    });

  const pending = items.filter((i) => i.decision === "pending");
  const decided = items.filter((i) => i.decision !== "pending");

  return (
    <div className="update-panel">
      <p className="eyebrow">Fraîcheur de la chaîne</p>
      <p>
        Recherchée le {frDate(f.researched_at)}
        {f.checked_at ? `, changements vérifiés le ${frDate(f.checked_at)}` : ""}
        {f.age_days !== null ? ` (il y a ${f.age_days} jour${f.age_days > 1 ? "s" : ""})` : ""}.
      </p>
      {f.advised && <p className="warn">Plus de {f.advised_after_days} jours : une mise à jour est conseillée.</p>}
      {f.past_dates.length > 0 && (
        <>
          <p className="warn">Échéances dépassées, à vérifier :</p>
          <ul className="plain warn">
            {f.past_dates.map((d, i) => (
              <li key={i}>
                <span className="mono">{d.date}</span> {clip(d.what, 160)}
              </li>
            ))}
          </ul>
        </>
      )}
      {f.shares_without_basis > 0 && (
        <p className="muted">
          {f.shares_without_basis} part{f.shares_without_basis > 1 ? "s" : ""} mondiale{f.shares_without_basis > 1 ? "s" : ""} sans nature (production,
          capacité ou exportations) : relire le rapport les précise.
        </p>
      )}
      {props.editable && !quote && !(job && job.state === "running") && (
        <div className="row">
          <button disabled={busy} onClick={() => ask("reread")} title="Relit le rapport déjà payé avec les règles actuelles, sans recherche web">
            Relire le rapport
          </button>
          <button disabled={busy} onClick={() => ask("search")} title="Courte recherche web sur ce qui a changé depuis la dernière recherche">
            Chercher les changements
          </button>
        </div>
      )}
      {quote && (
        <div className="quote">
          <p>
            {quote.kind === "search"
              ? `Recherche des changements depuis le ${frDate(f.checked_at ?? f.researched_at)} (${quote.searches_max} recherches web au plus), puis propositions.`
              : "Relecture du rapport enregistré avec les règles actuelles, sans recherche web."}{" "}
            Environ <strong className="mono">{usd(quote.estimate_usd)}</strong>, <span className="mono">{usd(quote.max_usd)}</span> au plus.
          </p>
          <p className="muted small">
            Plafond des reconstructions ce mois-ci : {usd(quote.remaining_usd)} restants sur {usd(quote.cap_usd)}. Rien ne change dans la chaîne sans ton accord,
            proposition par proposition.
          </p>
          <div className="row">
            <button className="primary" disabled={busy || quote.remaining_usd < quote.max_usd} onClick={launch}>
              Lancer
            </button>
            <button onClick={() => setQuote(null)}>Annuler</button>
          </div>
        </div>
      )}
      {job && (
        <p className={job.state === "failed" ? "warn" : "muted"}>
          {job.state === "running" ? `En cours : ${job.step_label}…` : clip(job.message, 300)}
          {job.state !== "running" && job.cost_usd > 0 ? ` Coût : ${usd(job.cost_usd)}.` : ""}
        </p>
      )}
      {error && <p className="warn">{error}</p>}
      {last?.injection_suspected && (
        <p className="warn">Une page lue pendant la mise à jour contenait des consignes adressées à l'IA : relis les sources avant d'accepter.</p>
      )}
      {pending.length > 0 && (
        <>
          <p className="eyebrow">Propositions à relire ({pending.length})</p>
          <ul className="plain proposals">
            {pending.map((item) => (
              <li key={item.id} className="proposal">
                <strong>{CHANGE_TYPE[item.change_type]}</strong>
                {item.node_key && item.change_type !== "new_producer" ? ` · ${clip(names.get(item.node_key) ?? item.node_key, 80)}` : ""}
                <span className="muted"> · confiance {CONFIDENCE[item.confidence].toLowerCase()}</span>
                <div>{clip(item.summary, 400)}</div>
                {detail(item) && <div className="muted small">{detail(item)}</div>}
                {item.anomalies.length > 0 && <div className="warn small">{item.anomalies.map((a) => clip(a, 120)).join(" ; ")}</div>}
                <SourceLinks refs={item.source_refs} sources={props.sources} />
                {props.editable && (
                  <div className="row">
                    <button className="primary" disabled={busy} onClick={() => decide(item, true)}>
                      Accepter
                    </button>
                    <button disabled={busy} onClick={() => decide(item, false)}>
                      Refuser
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {last && pending.length === 0 && (
        <p className="muted small">
          Dernière {last.kind === "search" ? "recherche des changements" : "relecture"} le {frDate(last.created_at)} :{" "}
          {decided.length === 0 ? "aucun changement proposé" : `${decided.filter((i) => i.decision === "accepted").length} acceptée(s), ${decided.filter((i) => i.decision === "rejected").length} refusée(s)`}.
        </p>
      )}
      {last && last.notes.length > 0 && (
        <details>
          <summary className="muted small">Points incertains ou écartés ({last.notes.length})</summary>
          <ul className="plain muted small">
            {last.notes.map((q, i) => (
              <li key={i}>{clip(q, 300)}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
