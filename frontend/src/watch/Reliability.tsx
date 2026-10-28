import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { clip, safeHref } from "../chain/types";
import { Locked, Pv, usePrivacy } from "../private/privacy";
import { AdviceChart } from "./AdviceChart";
import {
  RANK_LABEL,
  RESULT_LABEL,
  signed,
  type AdviceDossier,
  type Calibration,
  type LedgerAdvice,
  type Reliability as Data,
  type Replay,
} from "./types";

const GOOD = new Set(["confirmed", "correct_calm", "range_hit", "right_direction"]);
const MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."];
const KIND_LABEL: Record<string, string> = {
  verdict: "Verdict",
  hypothesis: "Hypothèse de prix",
  pressure: "Pression de prix",
  missed_move: "Hausse non signalée",
};

function frDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${MONTHS[m - 1]}`;
}

function shortHash(h: string | undefined): string {
  if (!h) return "—";
  return `${h.slice(0, 4)} ${h.slice(4, 8)} … ${h.slice(-4)}`;
}

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

/** The badge of an advice, as on the Markets screen. */
function AdviceBadge(props: { a: LedgerAdvice }) {
  const a = props.a;
  if (a.kind === "verdict") {
    const cls = a.verdict === "ant" ? "badge ant" : a.verdict === "pot" ? "badge pot" : a.verdict === "unverified" ? "badge unverified" : "badge neutral";
    const text = a.verdict === "pot" && a.x_weeks ? `Problème à ${String(a.x_weeks).replace(".", ",")} sem.` : a.label ?? a.verdict;
    return <span className={cls}>{text}</span>;
  }
  if (a.kind === "hypothesis") return <span className="badge neutral">Hypothèse {a.horizon}</span>;
  if (a.kind === "pressure") return <span className="badge neutral">Pression {signed(a.pressure ?? 0, 0)}</span>;
  return <span className="badge miss">Hausse non signalée</span>;
}

function ResultBadge(props: { a: LedgerAdvice }) {
  const o = props.a.outcome;
  if (props.a.kind === "missed_move") return <span className="result bad">✕ hausse manquée</span>;
  if (!o) return <span className="muted">en cours</span>;
  if (o.result === "unresolved") return <span className="muted">non résolu</span>;
  const good = GOOD.has(o.result);
  return (
    <span className={good ? "result good" : "result bad"}>
      {good ? "✓" : "✕"} {RESULT_LABEL[o.result] ?? o.result}
      {o.on_index ? " (sur indice)" : ""}
    </span>
  );
}

export function Reliability(props: { materialId: string | null; onMaterial: (id: string | null) => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [kind, setKind] = useState("");
  const [result, setResult] = useState("");
  const [version, setVersion] = useState("");

  useEffect(() => {
    setData(null);
    setPicked(null);
    const q = props.materialId ? `?material_id=${props.materialId}` : "";
    api.get<Data>(`/api/reliability${q}`).then(setData, (e) => setError(e instanceof ApiError ? e.message : "Erreur."));
  }, [props.materialId]);

  const advices = useMemo(() => data?.advices ?? [], [data]);
  const bySeq = useMemo(() => new Map(advices.map((a) => [a.seq, a])), [advices]);
  const versionDays = useMemo(() => (data?.version_changes ?? []).map((v) => v.day), [data]);
  // The advice shown by default: the latest verdict.
  const current = picked ?? [...advices].reverse().find((a) => a.kind === "verdict")?.seq ?? null;
  const selected = current !== null ? bySeq.get(current) ?? null : null;
  const highlighted = useMemo(() => (selected ? [selected.seq, ...(selected.band ? [selected.band.seq] : [])] : []), [selected]);
  const versions = useMemo(() => [...new Set(advices.map((a) => a.version).filter((v): v is string => !!v))], [advices]);
  const shown = useMemo(
    () =>
      [...advices].reverse().filter((a) => {
        if (kind && a.kind !== kind) return false;
        if (version && a.version !== version) return false;
        if (result === "pending") return !a.outcome && a.kind !== "missed_move";
        if (result === "good") return a.good === true;
        if (result === "bad") return a.good === false || a.kind === "missed_move";
        if (result === "unresolved") return a.outcome?.result === "unresolved";
        return true;
      }),
    [advices, kind, result, version],
  );

  if (!data) return <section className="panel">{error ?? "Chargement…"}</section>;
  const ind = data.indicators;
  const name = data.materials.find((m) => m.id === props.materialId)?.name;
  const ranges = ind.ranges_hit;
  const exportUrl = `/api/reliability/export.csv${props.materialId ? `?material_id=${props.materialId}` : ""}`;

  return (
    <div className="sheet reliability">
      <section className="panel">
        <div className="row spread">
          <div>
            <h2>Fiabilité de Goulot</h2>
            <p className="muted lead">
              Chaque conseil est inscrit au registre le jour où il est donné, puis comparé à ce qui s'est vraiment passé. Rien n'est
              réécrit après coup : les réussites et les erreurs restent visibles.
            </p>
          </div>
          <div className="row no-print">
            <a className="button" href={exportUrl} download>
              Exporter l'historique (CSV)
            </a>
            <button onClick={() => window.print()}>Imprimer (PDF)</button>
          </div>
        </div>
        <p className={data.ledger.ok ? "muted" : "warn"}>
          {data.ledger.ok
            ? `Registre intact · ${data.ledger.entries} entrées · empreinte ${data.ledger.head}…`
            : `Registre altéré à l'entrée n° ${data.ledger.at_seq} : ${data.ledger.problem}`}
        </p>
        <h3 className="title">Marché</h3>
        <p className="muted">Prix publics seuls. Chaque indicateur indique aussi la part des conseils résolus.</p>
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
      </section>

      <div className="no-print">
        <MyMaterial materialId={props.materialId} />
      </div>

      <div className="segmented tabs-wrap no-print" role="group" aria-label="Matière">
        <button className={props.materialId === null ? "tab active" : "tab"} aria-pressed={props.materialId === null} onClick={() => props.onMaterial(null)}>
          Toutes
        </button>
        {data.materials.map((m) => (
          <button key={m.id} className={props.materialId === m.id ? "tab active" : "tab"} aria-pressed={props.materialId === m.id} onClick={() => props.onMaterial(m.id)}>
            {m.name}
            {m.deleted ? " (supprimée)" : ""}
          </button>
        ))}
      </div>

      <div className="sheet-grid">
        <section className="panel">
          {data.series ? (
            <>
              <h3 className="title">{name ?? "Matière"} : prix réel et conseils datés</h3>
              <p className="muted">
                {data.series.name} ({RANK_LABEL[data.series.rank]}) · 12 derniers mois
                {ranges.resolved ? ` · ${ranges.good} fourchettes justes sur ${ranges.resolved}` : ""}
              </p>
              <AdviceChart
                series={data.series}
                advices={advices}
                versionDays={versionDays}
                today={data.today}
                selected={highlighted}
                onPick={setPicked}
              />
              <ChartLegend />
            </>
          ) : (
            <>
              <h3 className="title">Prix réel et conseils datés</h3>
              <p className="muted">
                {props.materialId
                  ? "Aucune série de prix pour cette matière : les conseils restent au registre ci-dessous."
                  : "Choisis une matière ci-dessus pour voir son prix réel et ses conseils sur le graphique."}
              </p>
            </>
          )}
        </section>
        <Dossier advice={selected} />
      </div>

      <div className="sheet-grid ledger-grid">
        <section className="panel">
          <div className="row spread">
            <h3 className="title">Registre des conseils</h3>
            <span className="muted">
              {shown.length} conseil(s) sur {data.ledger.entries} entrées · clique une ligne pour l'afficher sur le graphique
            </span>
          </div>
          <div className="row filters no-print">
            <select aria-label="Type de conseil" value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">Tous les types</option>
              {Object.entries(KIND_LABEL).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
            <select aria-label="Résultat" value={result} onChange={(e) => setResult(e.target.value)}>
              <option value="">Tous les résultats</option>
              <option value="good">Justes</option>
              <option value="bad">Manqués</option>
              <option value="unresolved">Non résolus</option>
              <option value="pending">En cours</option>
            </select>
            {versions.length > 1 && (
              <select aria-label="Version" value={version} onChange={(e) => setVersion(e.target.value)}>
                <option value="">Toutes les versions</option>
                {versions.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            )}
          </div>
          <div className="table-scroll">
            <table className="watchlist ledger-table">
              <thead>
                <tr>
                  <th>N°</th>
                  <th>Date</th>
                  {!props.materialId && <th>Matière</th>}
                  <th>Conseil</th>
                  <th>Annoncé</th>
                  <th>Échéance</th>
                  <th>Constaté</th>
                  <th>Résultat</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((a) => (
                  <tr
                    key={a.seq}
                    className={a.seq === current ? "clickable picked" : "clickable"}
                    onClick={() => setPicked(a.seq)}
                    aria-selected={a.seq === current}
                  >
                    <td className="mono muted">#{a.seq}</td>
                    <td className="mono">{frDay(a.kind === "missed_move" ? a.day : a.at)}</td>
                    {!props.materialId && (
                      <td>
                        <strong>{a.material_name}</strong>
                        {a.material_deleted && <span className="badge neutral">supprimée</span>}
                      </td>
                    )}
                    <td>
                      <AdviceBadge a={a} />
                    </td>
                    <td>{a.kind === "missed_move" ? "—" : a.announced}</td>
                    <td className="mono">
                      {a.deadline ? `${a.deadline_estimated ? "≈ " : ""}${frDay(a.deadline)}` : "—"}
                    </td>
                    <td className="mono observed">{a.observed}</td>
                    <td>
                      <ResultBadge a={a} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <CalibrationPanel c={data.calibration} />
      </div>
    </div>
  );
}

function ChartLegend() {
  return (
    <ul className="legend-line" aria-label="Légende">
      <li>
        <span className="lg-diamond" /> conseil émis
      </li>
      <li>
        <span className="lg-fan" /> fourchette annoncée, 20 jours de bourse
      </li>
      <li>
        <span className="lg-dot good">✓</span> juste
      </li>
      <li>
        <span className="lg-dot bad">✕</span> manqué
      </li>
      <li>
        <span className="lg-ring" /> hausse non signalée
      </li>
      <li>
        <span className="lg-ribbon" /> verdict en vigueur (gris : rien à signaler, jaune : problème, orange : anticiper)
      </li>
    </ul>
  );
}

/** The panel on the right: one advice in full, what Goulot knew that day, its hash, replay and sources. */
function Dossier(props: { advice: LedgerAdvice | null }) {
  const a = props.advice;
  const { vaultOpen } = usePrivacy();
  const [dossier, setDossier] = useState<AdviceDossier | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [check, setCheck] = useState<{ status: string; message: string } | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sources, setSources] = useState(false);

  useEffect(() => {
    setDossier(null);
    setReplay(null);
    setCheck(null);
    setReplayError(null);
    setSources(false);
    if (!a || a.kind === "missed_move") return;
    api.get<AdviceDossier>(`/api/reliability/advice/${a.seq}`).then(setDossier, () => setDossier(null));
  }, [a]);

  if (!a) {
    return (
      <section className="panel">
        <h3 className="title">Dossier du conseil</h3>
        <p className="muted">Aucun conseil au registre pour l'instant.</p>
      </section>
    );
  }

  async function doReplay() {
    if (!a) return;
    setBusy(true);
    setReplayError(null);
    try {
      setReplay(await api.get<Replay>(`/api/reliability/advice/${a.seq}/replay`));
      if (vaultOpen) setCheck(await api.get(`/api/private/reliability/advice/${a.seq}/inputs-check`));
    } catch (e) {
      setReplayError(e instanceof ApiError ? e.message : "Rejeu impossible.");
    } finally {
      setBusy(false);
    }
  }

  const band = a.band;
  const lead = a.outcome?.result === "confirmed" && a.outcome.lead_weeks !== null ? a.outcome.lead_weeks : null;
  const allSources = (dossier?.knew ?? []).flatMap((k) => k.sources);
  return (
    <section className="panel dossier">
      <div className="row spread">
        <h3 className="title">Dossier du conseil</h3>
        <span className="mono muted">#{a.seq}</span>
      </div>
      <h3>{a.material_name || "—"}</h3>
      <p className="muted">émis le {frDay(a.kind === "missed_move" ? a.day : a.at)}</p>
      <div className="row">
        <AdviceBadge a={a} />
        <ResultBadge a={a} />
      </div>
      <dl className="facts">
        <dt>Annoncé</dt>
        <dd>{a.kind === "missed_move" ? "rien : aucune alerte dans les 8 semaines avant" : a.announced}</dd>
        {a.deadline && (
          <>
            <dt>Fenêtre</dt>
            <dd>
              du {frDay(a.at)} au {a.deadline_estimated ? "vers le " : ""}
              {frDay(a.deadline)}
            </dd>
          </>
        )}
        <dt>Constaté</dt>
        <dd className="mono">{a.observed}</dd>
        {band && (
          <>
            <dt>Fourchette</dt>
            <dd>
              {signed(band.p10)} % à {signed(band.p90)} % sous 20 jours, probabilité {Math.round(band.probability * 100)} %
              {band.observed_pct !== null ? ` · constaté ${signed(band.observed_pct)} %` : ""}
              {band.hit === null ? "" : band.hit ? " : juste" : " : manquée"}
            </dd>
          </>
        )}
        {lead !== null && (
          <>
            <dt>Avance</dt>
            <dd>{String(lead).replace(".", ",")} semaines avant le franchissement du seuil</dd>
          </>
        )}
        {a.version && (
          <>
            <dt>Versions</dt>
            <dd className="mono small">{a.version}</dd>
          </>
        )}
      </dl>
      {a.kind !== "missed_move" && (
        <>
          <p className="eyebrow">Ce que Goulot savait ce jour-là</p>
          {dossier === null ? (
            <p className="muted">Chargement…</p>
          ) : dossier.knew.length === 0 ? (
            <p className="muted">Aucun événement : conseil fondé sur l'absence de signal.</p>
          ) : (
            <ul className="knew">
              {dossier.knew.map((k) => (
                <li key={k.id}>
                  {clip(k.title, 160)}
                  <span className="muted">
                    {" "}
                    · {k.confirmed === null ? "" : k.confirmed ? "confirmé" : "signal précoce"}
                    {k.first_seen ? `, vu le ${frDay(k.first_seen)}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      <hr />
      <p className="muted small">Empreinte au registre</p>
      <p className="mono">
        {shortHash(a.hash)} · chaînée à {a.seq > 1 ? `#${a.seq - 1}` : "l'origine"}
      </p>
      <div className="row no-print">
        {a.kind === "verdict" && (
          <button className="primary" disabled={busy} onClick={() => void doReplay()}>
            {busy ? "Rejeu…" : "Rejouer cette journée"}
          </button>
        )}
        {allSources.length > 0 && <button onClick={() => setSources(!sources)}>{sources ? "Masquer les sources" : "Voir les sources"}</button>}
      </div>
      {replayError && <p className="warn">{replayError}</p>}
      {replay && (
        <div className={replay.same ? "replay same" : "replay differs"}>
          <p>
            <strong>{replay.same ? "✓ Même verdict recalculé" : "✕ Verdict différent au recalcul"}</strong>
          </p>
          <p>
            Registre : {replay.recorded}
            <br />
            Recalcul : {replay.replayed}
            {replay.same ? "" : ` (diffère sur : ${replay.differences.join(", ")})`}
          </p>
          <p className="muted small">
            {replay.events} événement(s) et {replay.chain_nodes} maillon(s) en vigueur ce jour-là. {replay.caveat}
          </p>
          {check ? (
            <p className={check.status === "intact" ? "small" : "warn small"}>
              {check.status === "intact" ? "✓ " : "✕ "}
              {check.message}
            </p>
          ) : (
            !vaultOpen && <p className="muted small">Coffre ouvert, le rejeu vérifie aussi l'empreinte des données du calcul.</p>
          )}
        </div>
      )}
      {sources && (
        <ul className="plain">
          {allSources.map((s, i) => {
            const href = safeHref(s.url);
            return (
              <li key={`${s.url}-${i}`}>
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer" className="source">
                    {clip(s.title || s.publisher, 120)}
                  </a>
                ) : (
                  clip(s.title || s.publisher, 120)
                )}
                {s.publisher && <span className="muted"> · {clip(s.publisher, 60)}</span>}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** « Quand Goulot annonce 60 %, est-ce 60 % ? »: Brier score from 30 resolved hypotheses, diagram from 50. */
function CalibrationPanel(props: { c: Calibration }) {
  const c = props.c;
  const size = 240;
  const pad = 34;
  const span = size - 2 * pad;
  const px = (p: number) => pad + p * span;
  const py = (p: number) => size - pad - p * span;
  const most = Math.max(1, ...c.bins.map((b) => b.n));
  return (
    <section className="panel">
      <h3 className="title">Quand Goulot annonce 60 %, est-ce 60 % ?</h3>
      <p className="muted">
        {c.resolved} hypothèse(s) de prix résolue(s). Score à partir de 30, diagramme à partir de 50.
      </p>
      {c.show_diagram ? (
        <svg className="calibration" viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Diagramme de calibration">
          <line x1={px(0)} y1={py(0)} x2={px(1)} y2={py(1)} className="diag" />
          <line x1={px(0)} y1={py(0)} x2={px(1)} y2={py(0)} className="axis" />
          <line x1={px(0)} y1={py(0)} x2={px(0)} y2={py(1)} className="axis" />
          <text x={px(0)} y={size - 14} textAnchor="middle">0</text>
          <text x={px(0.5)} y={size - 14} textAnchor="middle">50 %</text>
          <text x={px(1)} y={size - 14} textAnchor="middle">100 %</text>
          <text x={pad - 6} y={py(0.5) + 4} textAnchor="end">50 %</text>
          <text x={pad - 6} y={py(1) + 4} textAnchor="end">100 %</text>
          <text x={px(0.5)} y={size - 2} textAnchor="middle" className="axis-label">probabilité annoncée</text>
          {c.bins.map((b) => (
            <circle key={b.p} cx={px(b.p)} cy={py(b.observed)} r={4 + 9 * Math.sqrt(b.n / most)} className="bin">
              <title>{`${Math.round(b.p * 100)} % annoncé, ${Math.round(b.observed * 100)} % constaté, ${b.n} hypothèse(s)`}</title>
            </circle>
          ))}
        </svg>
      ) : null}
      {c.show_brier && c.brier !== null && (
        <dl className="facts">
          <dt>Score de Brier</dt>
          <dd className="mono">{c.brier.toFixed(3).replace(".", ",")}</dd>
          <dt>Taux de base</dt>
          <dd className="mono">{(c.base_brier ?? 0).toFixed(3).replace(".", ",")}</dd>
        </dl>
      )}
      <p>{c.verdict}</p>
      <p className="eyebrow">Comment c'est compté</p>
      <ul className="how">
        <li>
          Une fourchette est juste si la variation constatée, 20 jours de bourse plus tard, tombe dedans. Elle couvre 80 % des cas
          annoncés : la cible est donc 80 % de fourchettes justes.
        </li>
        <li>
          « Anticiper stock » ou « Problème à X semaines » est confirmé si la hausse atteint le seuil (5 % par défaut) dans la fenêtre
          annoncée, à 2 semaines près.
        </li>
        <li>« Rien à signaler » est confirmé si aucune hausse du seuil n'arrive dans les 8 semaines ; sinon, c'est une hausse manquée.</li>
        <li>Sans prix observé, un conseil reste « non résolu » : le silence ne compte jamais comme une réussite.</li>
        <li>
          Le score de Brier mesure l'écart entre la probabilité annoncée et ce qui s'est passé (0 : parfait). Le taux de base est le
          score qu'on obtiendrait en annonçant toujours la fréquence moyenne.
        </li>
        <li>Chaque entrée est chaînée à la précédente par une empreinte. Une modification ou une suppression se voit aussitôt.</li>
      </ul>
    </section>
  );
}

type Personal = {
  indicators: {
    break_in: boolean;
    warnings_confirmed: { good: number; resolved: number; matured: number; on_index: number };
    calm_right: { good: number; resolved: number; matured: number; on_index: number };
    pressure_right: { good: number; resolved: number; matured: number; on_index: number };
    false_alarms: number;
    missed_rises: number;
    average_lead_weeks: number | null;
    resolved: number;
    by_answer: number;
    by_private_series: number;
  };
  entries: { seq: number; at: string; kind: string; verdict?: string; label?: string; result?: string; basis?: string; of_seq?: number; answer?: string }[];
};

/** « Ma matière » column: personal verdicts, judged on the user's answers, private series and public prices. Only
 * with the vault open. */
function MyMaterial(props: { materialId: string | null }) {
  const { vaultOpen } = usePrivacy();
  const [data, setData] = useState<Personal | null>(null);
  useEffect(() => {
    if (!vaultOpen) {
      setData(null);
      return;
    }
    const q = props.materialId ? `?material_id=${props.materialId}` : "";
    api.get<Personal>(`/api/private/reliability${q}`).then(setData, () => setData(null));
  }, [vaultOpen, props.materialId]);
  if (!vaultOpen) {
    return (
      <section className="panel">
        <h2>Fiabilité · Ma matière</h2>
        <Locked text="La colonne « Ma matière » (verdicts personnels) ne s'affiche que coffre ouvert." />
      </section>
    );
  }
  if (!data) return <section className="panel">Chargement…</section>;
  const ind = data.indicators;
  const outcomes = new Map(data.entries.filter((e) => e.kind === "outcome").map((e) => [e.of_seq, e]));
  return (
    <section className="panel">
      <h2>Fiabilité · Ma matière</h2>
      {ind.break_in && <p className="muted">Pas encore assez de recul ({ind.resolved} conseils personnels résolus).</p>}
      <div className="kpis">
        <Ratio label="Alertes confirmées" stat={ind.warnings_confirmed} breakIn={ind.break_in} />
        <Ratio label="« Rien à signaler » justes" stat={ind.calm_right} breakIn={ind.break_in} />
        <Ratio label="Pressions dans le bon sens" stat={ind.pressure_right} breakIn={ind.break_in} />
        <div className="kpi">
          <span className="muted">Fausses alertes · hausses manquées</span>
          <strong className="mono">
            {ind.false_alarms} · {ind.missed_rises}
          </strong>
          <span className="muted">
            {ind.by_answer} par tes réponses · {ind.by_private_series} par tes devis et achats
          </span>
        </div>
      </div>
      <table className="watchlist">
        <tbody>
          {data.entries
            .filter((e) => e.kind === "verdict" || e.kind === "signal" || e.kind === "missed_move")
            .reverse()
            .map((e) => {
              const o = outcomes.get(e.seq);
              return (
                <tr key={e.seq}>
                  <td className="mono muted">{e.seq}</td>
                  <td className="mono">{e.at.slice(0, 10)}</td>
                  <td>
                    <Pv>{e.kind === "verdict" ? e.label ?? e.verdict : e.kind === "signal" ? "Signal fournisseur" : "Hausse non signalée"}</Pv>
                  </td>
                  <td>
                    {o ? (
                      <span className={GOOD.has(o.result ?? "") ? "up" : o.result === "unresolved" ? "muted" : "down"}>
                        {RESULT_LABEL[o.result ?? ""] ?? o.result} {o.basis ? `(${o.basis})` : ""}
                      </span>
                    ) : (
                      <span className="muted">{e.kind === "verdict" ? "en cours" : "—"}</span>
                    )}
                  </td>
                </tr>
              );
            })}
        </tbody>
      </table>
    </section>
  );
}
