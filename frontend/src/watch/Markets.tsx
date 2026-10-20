import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { clip } from "../chain/types";
import { RANK_LABEL, signed, verdictClass, type Freshness, type MarketRow, type SeriesPayload } from "./types";

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
  const [fresh, setFresh] = useState<Freshness | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      api.get<{ materials: MarketRow[]; freshness: Freshness }>("/api/markets").then(
        (d) => {
          setRows(d.materials);
          setFresh(d.freshness);
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
      <div className="row spread">
        <h2>Marchés</h2>
        <span className={fresh?.verified ? "muted" : "warn"}>
          {fresh?.last_ok_cycle
            ? `Dernière veille réussie : ${new Date(fresh.last_ok_cycle).toLocaleString("fr-FR")}`
            : "Aucune veille réussie pour l'instant"}
          {fresh && !fresh.verified ? " · non vérifié" : ""}
        </span>
      </div>
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
              return (
                <tr key={r.id} className={ready ? "clickable" : ""} onClick={() => (ready ? props.onOpen(r.id) : props.onChain(r.id))}>
                  <td>{clip(r.name, 60)}</td>
                  <td className="mono muted">{r.cas}</td>
                  <td className="mono">
                    {last ? last.value.toLocaleString("fr-FR", { maximumFractionDigits: 2 }) : "—"}
                    {r.series && <span className="muted"> · {RANK_LABEL[r.series.rank]}</span>}
                  </td>
                  <td className={`mono ${(r.series?.change_pct ?? 0) >= 0 ? "up" : "down"}`}>{signed(r.series?.change_pct)} %</td>
                  <td>{r.series ? <Sparkline series={r.series} /> : <span className="muted">—</span>}</td>
                  <td>
                    {ready ? (
                      <span className={verdictClass(r.verdict)}>{r.verdict?.label ?? "Pas encore de verdict"}</span>
                    ) : (
                      <span className="badge neutral">Chaîne à valider</span>
                    )}
                  </td>
                  <td>
                    {r.personal ? (
                      <span className={`badge ${r.personal.verdict}`} title={r.personal.reason}>
                        {r.personal.label}
                      </span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>{r.verdict ? <Pressure value={r.verdict.pressure} label={r.verdict.pressure_label} /> : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <button className="primary" onClick={props.onAdd}>
        Ajouter une matière
      </button>
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
