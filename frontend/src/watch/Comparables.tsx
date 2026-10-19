import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { clip } from "../chain/types";
import { scale, signed, type Comparables as Data } from "./types";

// « Cas comparables » card (design/README.md): one dot per past case on its own change at 20 sessions, the 10 % to
// 90 % band and the median of the cases kept, and the AI's estimate above. Excluded cases (overlap, draft) stay
// visible but are not counted. Always « observée après », never « causée par ».

const W = 640;
const X0 = 40;
const X1 = 600;
const STATE_LABEL = { kept: "retenu", overlap: "exclu : chevauchement", draft: "brouillon IA à vérifier" };

export function Comparables(props: { eventId: string | null; onLibrary: () => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    setMessage(null);
    if (!props.eventId) return;
    api
      .get<Data>(`/api/events/${encodeURIComponent(props.eventId)}/analogues`)
      .then(setData)
      .catch((e) => setMessage(e instanceof ApiError ? e.message : "Erreur."));
  }, [props.eventId]);

  return (
    <section className="panel comparables" aria-label="Cas comparables">
      <div className="row spread">
        <div>
          <h3 className="title">Cas comparables</h3>
          {data && <span className="muted">{clip(data.event.title, 120)}</span>}
        </div>
        <button className="tab" onClick={props.onLibrary}>
          Bibliothèque de cas
        </button>
      </div>
      {!props.eventId && <p className="muted">Aucun événement sélectionné : rien à comparer.</p>}
      {message && <p className="warn">{message}</p>}
      {data && <Card data={data} />}
      <p className="muted small">
        Variation propre : variation à 20 séances après la première annonce, hors mouvement général du marché, calculée
        par le code sur les séries de la matière. « Hausse observée après », jamais « causée par ».
      </p>
    </section>
  );
}

function Card({ data }: { data: Data }) {
  // Sorted by value, so that labels of neighbouring dots alternate above and below.
  const dots = data.rows.filter((r) => r.own_20 !== null).sort((a, b) => (a.own_20 as number) - (b.own_20 as number));
  const values = [...dots.map((r) => r.own_20 as number)];
  if (data.band) values.push(data.band.low, data.band.high);
  if (data.estimate && data.estimate.unit === "percent") values.push(data.estimate.low, data.estimate.high);
  const at = scale(values);
  const x = (v: number) => X0 + (X1 - X0) * at(v);
  const ticks = values.length ? niceTicks(Math.min(0, ...values), Math.max(0, ...values)) : [];
  const estimate = data.estimate && data.estimate.unit === "percent" ? data.estimate : null;

  return (
    <>
      {data.band ? (
        <div className="comparables-body">
          <svg viewBox={`0 0 ${W} 128`} role="img" aria-label={data.summary} className="comparables-chart">
            <rect className="band" x={x(data.band.low)} y={34} width={Math.max(2, x(data.band.high) - x(data.band.low))} height={60} rx={6} />
            <line className="zero" x1={x(0)} x2={x(0)} y1={30} y2={98} />
            <line className="axis" x1={X0} x2={X1} y1={104} y2={104} />
            {ticks.map((t) => (
              <text key={t} className="tick" x={x(t)} y={120} textAnchor="middle">
                {signed(t, 0)} %
              </text>
            ))}
            <line className="median" x1={x(data.band.median)} x2={x(data.band.median)} y1={34} y2={94} />
            {estimate && (
              <g className="estimate">
                <line x1={x(estimate.low)} x2={x(estimate.high)} y1={22} y2={22} />
                <line x1={x(estimate.low)} x2={x(estimate.low)} y1={17} y2={27} />
                <line x1={x(estimate.high)} x2={x(estimate.high)} y1={17} y2={27} />
                <text x={(x(estimate.low) + x(estimate.high)) / 2} y={12} textAnchor="middle">
                  estimation de l'IA
                </text>
              </g>
            )}
            {dots.map((r, i) => (
              <g key={r.id}>
                <circle className={`dot ${r.state}`} cx={x(r.own_20 as number)} cy={64} r={6} />
                <text className="tick" x={x(r.own_20 as number)} y={i % 2 ? 88 : 46} textAnchor="middle">
                  {r.first_report_date.slice(0, 4)}
                </text>
              </g>
            ))}
          </svg>
          <div>
            <p className="headline">{data.summary}</p>
            <p className="muted">
              Médiane {signed(data.band.median)} %, de {signed(data.band.low)} à {signed(data.band.high)} % (10 % à 90 % des cas retenus).
            </p>
            {data.estimate_outside && <p className="warn">L'estimation de l'IA sort de la fourchette des cas comparables.</p>}
          </div>
        </div>
      ) : (
        <p className="notice">{data.rows.length === 0 ? "Aucun cas passé assez proche dans la bibliothèque." : data.summary}</p>
      )}
      {data.rows.length > 0 && (
        <ul className="plain case-rows">
          {data.rows.map((r) => (
            <li key={r.id} className={`case-row ${r.state}`}>
              <span className="mono muted">{r.first_report_date.slice(0, 4)}</span>
              <span>
                {clip(r.title, 140)}
                <span className="muted small">
                  {" "}
                  · {clip(r.place, 60)} · part de capacité : {r.capacity_share_pct === null ? "inconnue" : `${r.capacity_share_pct.toFixed(1).replace(".", ",")} %`} ·{" "}
                  {STATE_LABEL[r.state]}
                </span>
              </span>
              <span className={`mono ${(r.own_20 ?? 0) >= 0 ? "up" : "down"}`}>{r.own_20 === null ? "—" : `${signed(r.own_20)} %`}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function niceTicks(lo: number, hi: number): number[] {
  const span = Math.max(1, hi - lo);
  const step = [1, 2, 5, 10, 20, 50].find((s) => span / s <= 5) ?? 100;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) out.push(t);
  return out;
}
