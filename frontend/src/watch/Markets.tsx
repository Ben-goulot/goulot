import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { clip } from "../chain/types";
import { DeleteMaterial, setWatched } from "./Lifecycle";
import { WorldHotspots } from "./WorldHotspots";
import { RANK_LABEL, signed, verdictClass, watchLabel, type MarketRow, type SeriesPayload } from "./types";

const POLL_MS = 60_000;

/** Inline SVG sparkline: attributes only, no style attribute (CSP style-src 'self'). */
export function Sparkline(props: { series: SeriesPayload }) {
  const values = props.series.points.map((p) => p.value);
  if (values.length < 2) return <span className="muted">—</span>;
  const min = Math.min(...values);
  const span = Math.max(...values) - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 96 + 2},${30 - ((v - min) / span) * 26}`).join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg className="spark" viewBox="0 0 100 32" width="100" height="32" aria-hidden="true">
      <polyline points={pts} fill="none" stroke={up ? "#3CCB91" : "#FF7B7B"} strokeWidth="1.5" />
    </svg>
  );
}

export function Markets(props: { onOpen: (id: string) => void; onChain: (id: string) => void; onAdd: () => void }) {
  const [rows, setRows] = useState<MarketRow[] | null>(null);
  const [archived, setArchived] = useState<{ id: string; name: string; cas: string; open_advice: number }[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      api.get<{ materials: MarketRow[]; archived: typeof archived }>("/api/markets").then(
        (d) => {
          setRows(d.materials);
          setArchived(d.archived ?? []);
        },
        (e) => setError(e instanceof ApiError ? e.message : "Erreur."),
      );
    void load();
    const id = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(id);
  }, []);

  if (!rows) return <section className="panel">{error ?? "Chargement…"}</section>;
  const breakIn = rows.some((r) => r.verdict?.break_in);

  return (
    <section className="panel">
      <h2>Marchés</h2>
      {breakIn && <p className="muted">Rodage : les verdicts des 90 premiers jours sont à prendre avec recul.</p>}
      {rows.length === 0 ? (
        <p className="muted">Aucune matière suivie pour l'instant.</p>
      ) : (
        <table className="watchlist">
          <thead>
            <tr>
              <th>Matière</th>
              <th>CAS</th>
              <th>Prix ou indice</th>
              <th>Variation</th>
              <th>2 mois</th>
              <th>Verdict</th>
              <th>Pour toi</th>
              <th>Pression</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const last = r.series?.points[r.series.points.length - 1];
              const ready = r.status === "validated";
              const watch = watchLabel(r.watch);
              const change = r.series?.change_pct;
              const open = () => (ready ? props.onOpen(r.id) : props.onChain(r.id));
              return (
                <tr
                  key={r.id}
                  className="clickable"
                  tabIndex={0}
                  aria-label={`${r.name} : ouvrir ${ready ? "la fiche" : "la chaîne"}`}
                  onClick={open}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      open();
                    }
                  }}
                >
                  <td className="cell-name">
                    {clip(r.name, 60)}
                    <div className={`small ${watch.warn ? "warn" : "muted"}`}>{watch.text}</div>
                  </td>
                  <td className="mono muted" data-label="CAS">{r.cas}</td>
                  <td className="mono" data-label="Prix ou indice">
                    {last ? last.value.toLocaleString("fr-FR", { maximumFractionDigits: 2 }) : "—"}
                    {r.series && <span className="muted"> · {RANK_LABEL[r.series.rank]}</span>}
                  </td>
                  {change === null || change === undefined ? (
                    <td className="mono muted" data-label="Variation">—</td>
                  ) : (
                    <td className={`mono ${change >= 0 ? "up" : "down"}`} data-label="Variation">{signed(change)} %</td>
                  )}
                  <td data-label="2 mois">{r.series ? <Sparkline series={r.series} /> : <span className="muted">—</span>}</td>
                  <td data-label="Verdict">
                    {ready ? (
                      <span className={verdictClass(r.verdict)}>{r.verdict?.label ?? "Pas encore de verdict"}</span>
                    ) : (
                      <span className="badge neutral">Chaîne à valider</span>
                    )}
                  </td>
                  <td data-label="Pour toi">
                    {r.personal ? (
                      <span className={`badge ${r.personal.verdict}`} title={r.personal.reason}>
                        {r.personal.label}
                      </span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td data-label="Pression">{r.verdict ? <Pressure value={r.verdict.pressure} label={r.verdict.pressure_label} /> : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {rows.length > 0 && (
        <ul className="legend-line verdict-legend" aria-label="Lire les verdicts">
          <li>
            <span className="badge neutral">Rien à signaler</span> aucun goulot qui pèse sur la matière
          </li>
          <li>
            <span className="badge pot">Problème potentiel à X semaines</span> surveiller : tu as le temps de te couvrir
          </li>
          <li>
            <span className="badge ant">Anticiper stock</span> agir maintenant : sécuriser du stock
          </li>
        </ul>
      )}
      <button className="primary" onClick={props.onAdd}>
        Ajouter une matière
      </button>
      <WorldHotspots />
      {archived.length > 0 && (
        <>
          <h3>Archivées</h3>
          <ul className="plain">
            {archived.map((m) => (
              <li key={m.id}>
                {clip(m.name, 60)} <span className="mono muted">{m.cas}</span>
                {m.open_advice > 0 && <span className="muted"> · prix suivis jusqu'à l'échéance de {m.open_advice} conseil(s)</span>}{" "}
                <button onClick={async () => { setError(await setWatched(m.id, true)); window.location.reload(); }}>Reprendre le suivi</button>
                <DeleteMaterial materialId={m.id} name={m.name} onDone={() => window.location.reload()} />
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export function Pressure(props: { value: number; label: string }) {
  const arrow = props.value > 1 ? "⇈" : props.value === 1 ? "↑" : props.value === 0 ? "→" : props.value === -1 ? "↓" : "⇊";
  const cls = props.value >= 2 ? "pressure strong" : props.value === 1 ? "pressure up" : "pressure neutral";
  return (
    <span className={cls}>
      {arrow} {props.label}
    </span>
  );
}
