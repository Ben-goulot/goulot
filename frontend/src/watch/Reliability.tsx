import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { PriceChart, type ChartMark } from "./PriceChart";
import { RANK_LABEL, RESULT_LABEL, signed, type Reliability as Data } from "./types";

const GOOD = new Set(["confirmed", "correct_calm", "range_hit", "right_direction"]);

function Ratio(props: { label: string; stat: { good: number; resolved: number; matured: number; on_index: number }; breakIn: boolean }) {
  const { good, resolved, matured, on_index } = props.stat;
  return (
    <div className="kpi">
      <span className="muted">{props.label}</span>
      <strong className="mono">{props.breakIn ? `${good} sur ${resolved}` : resolved ? `${Math.round((100 * good) / resolved)} %` : "—"}</strong>
      <span className="muted">
        résolus {resolved} sur {matured} arrivés à échéance{on_index ? ` · ${on_index} sur indice` : ""}
      </span>
    </div>
  );
}

export function Reliability(props: { materialId: string | null }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const q = props.materialId ? `?material_id=${props.materialId}` : "";
    api.get<Data>(`/api/reliability${q}`).then(setData, (e) => setError(e instanceof ApiError ? e.message : "Erreur."));
  }, [props.materialId]);

  const marks = useMemo<ChartMark[]>(
    () =>
      (data?.advices ?? [])
        .filter((a) => a.kind === "verdict" || a.kind === "missed_move")
        .map((a) => {
          if (a.kind === "missed_move") return { day: a.day ?? a.at.slice(0, 10), text: "hausse manquée", color: "#FF7B7B", shape: "circle" as const };
          const ok = a.outcome ? GOOD.has(a.outcome.result) : null;
          const color = a.verdict === "ant" ? "#FF8A4C" : a.verdict === "pot" ? "#F5B841" : "#B8C2CF";
          const text = `${a.label ?? a.verdict}${ok === null ? "" : ok ? " · juste" : " · manqué"}`;
          return { day: a.at.slice(0, 10), text, color, shape: "square" as const };
        }),
    [data],
  );

  if (!data) return <section className="panel">{error ?? "Chargement…"}</section>;
  const ind = data.indicators;

  return (
    <div className="sheet">
      <section className="panel">
        <div className="row spread">
          <h2>Fiabilité · Marché</h2>
          <span className={data.ledger.ok ? "muted" : "warn"}>
            {data.ledger.ok
              ? `Registre intact : ${data.ledger.entries} entrées, empreinte ${data.ledger.head}…`
              : `Registre altéré à l'entrée n° ${data.ledger.at_seq} : ${data.ledger.problem}`}
          </span>
        </div>
        {ind.break_in ? (
          <p className="muted">
            Pas encore assez de recul : rodage de 90 jours ou moins de 30 conseils résolus ({ind.resolved} pour l'instant). Les
            chiffres bruts restent visibles, sans pourcentage.
          </p>
        ) : null}
        <div className="kpis">
          <Ratio label="Alertes confirmées" stat={ind.warnings_confirmed} breakIn={ind.break_in} />
          <Ratio label="« Rien à signaler » justes" stat={ind.calm_right} breakIn={ind.break_in} />
          <Ratio label="Pressions de prix dans le bon sens" stat={ind.pressure_right} breakIn={ind.break_in} />
          <Ratio label="Fourchettes justes (cible 80 %)" stat={ind.ranges_hit} breakIn={ind.break_in} />
          <div className="kpi">
            <span className="muted">Fausses alertes · hausses manquées</span>
            <strong className="mono">
              {ind.false_alarms} · {ind.missed_rises}
            </strong>
            <span className="muted">avance moyenne {ind.average_lead_weeks ?? "—"} sem.</span>
          </div>
        </div>
        <p className="muted">La colonne « Ma matière » (verdicts personnels) arrive avec Ma chaîne, coffre ouvert.</p>
      </section>

      {data.series && (
        <section className="panel">
          <h3 className="title">
            Prix réel et conseils datés · {data.series.name} ({RANK_LABEL[data.series.rank]})
          </h3>
          <PriceChart series={data.series} marks={marks} height={300} />
        </section>
      )}

      <section className="panel">
        <p className="eyebrow">Registre</p>
        <table className="watchlist">
          <thead>
            <tr>
              <th>N°</th>
              <th>Date</th>
              <th>Conseil</th>
              <th>Résultat</th>
            </tr>
          </thead>
          <tbody>
            {[...data.advices].reverse().map((a) => (
              <tr key={a.seq}>
                <td className="mono muted">{a.seq}</td>
                <td className="mono">{a.at.slice(0, 10)}</td>
                <td>
                  {a.kind === "verdict" && (a.label ?? a.verdict)}
                  {a.kind === "pressure" && `Pression ${signed(a.pressure ?? 0, 0)}`}
                  {a.kind === "hypothesis" &&
                    `${a.instrument_id} : ${signed(a.move_p10)} à ${signed(a.move_p90)} à ${a.horizon}, p = ${(a.probability ?? 0).toFixed(2)}`}
                  {a.kind === "missed_move" && "Hausse non signalée"}
                </td>
                <td>
                  {a.outcome ? (
                    <span className={GOOD.has(a.outcome.result) ? "up" : a.outcome.result === "unresolved" ? "muted" : "down"}>
                      {RESULT_LABEL[a.outcome.result] ?? a.outcome.result}
                      {a.outcome.on_index ? " (sur indice)" : ""}
                    </span>
                  ) : (
                    <span className="muted">{a.kind === "missed_move" ? "—" : "en cours"}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
