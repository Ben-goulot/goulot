import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { clip } from "../chain/types";
import { Comparables } from "./Comparables";
import { EventMap } from "./EventMap";
import { Pressure } from "./Markets";
import { PriceChart, type ChartMark } from "./PriceChart";
import { RANK_LABEL, signed, verdictClass, type Sheet as SheetData } from "./types";

const STATUS_COLOR: Record<string, string> = { ongoing: "#FF8A4C", preparing: "#F5B841", early: "#B8C2CF" };
const SOURCES = [
  ["fred", "FRED"],
  ["bls_ppi", "BLS (indice)"],
  ["eurostat", "Eurostat (indice)"],
  ["yahoo_chart", "Yahoo (dépannage)"],
  ["stooq", "Stooq (dépannage)"],
];
const ROLES = [
  ["direct", "cotation de la matière"],
  ["official", "prix officiel"],
  ["index", "indice de famille"],
  ["input", "série d'un intrant"],
];

export function Sheet(props: {
  materialId: string;
  onChain: () => void;
  onReliability: () => void;
  onCases: () => void;
  onBack: () => void;
}) {
  const [data, setData] = useState<SheetData | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [form, setForm] = useState({ source: "fred", code: "", role: "direct", name: "" });

  const load = useCallback(async () => {
    try {
      setData(await api.get<SheetData>(`/api/materials/${props.materialId}/sheet`));
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  }, [props.materialId]);

  useEffect(() => {
    void load();
  }, [load]);

  const marks = useMemo<ChartMark[]>(
    () => (data?.events ?? []).map((e) => ({ day: e.first_seen.slice(0, 10), text: e.letter, color: STATUS_COLOR[e.status], shape: "circle" })),
    [data],
  );

  if (!data) return <section className="panel">{message ?? "Chargement…"}</section>;
  const v = data.verdict;

  const addLink = async () => {
    try {
      await api.post(`/api/materials/${props.materialId}/links`, form);
      setForm({ ...form, code: "", name: "" });
      setMessage("Série ajoutée : elle sera téléchargée au prochain cycle de veille.");
      await load();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };

  const validate = async (instrument: string) => {
    await api.post(`/api/materials/${props.materialId}/links/${encodeURIComponent(instrument)}/validate`);
    await load();
  };

  return (
    <div className="sheet">
      <header className="sheet-head">
        <button className="tab" onClick={props.onBack}>
          ← Marchés
        </button>
        <h2>{clip(data.material.name, 80)}</h2>
        <span className="mono muted">CAS {data.material.cas}</span>
        <button className="tab" onClick={props.onChain}>
          Voir la chaîne
        </button>
        <span className={data.freshness.verified ? "muted" : "warn"}>
          {data.freshness.last_ok_cycle ? `Vérifié le ${new Date(data.freshness.last_ok_cycle).toLocaleString("fr-FR")}` : "Jamais vérifié"}
        </span>
        {v?.break_in ? <span className="badge neutral">rodage</span> : null}
      </header>
      {message && <p className="banner">{message}</p>}

      <div className="sheet-grid">
        <section className="panel">
          <div className="row spread">
            <h3 className="title">{data.series ? `${clip(data.series.name, 60)} · ${RANK_LABEL[data.series.rank]}` : "Aucune série de prix"}</h3>
            {data.series && <span className={`mono ${(data.series.change_pct ?? 0) >= 0 ? "up" : "down"}`}>{signed(data.series.change_pct)} % sur 6 mois</span>}
          </div>
          {data.series ? (
            <PriceChart series={data.series} marks={marks} />
          ) : (
            <p className="muted">Ajoute ou valide une série ci-dessous ; sans série, la pression de prix reste affichée, sans pourcentage.</p>
          )}
          {data.margin && (
            <p className="muted">
              Marge sur les intrants (indice) :{" "}
              <span className={data.margin.trend === "up" ? "up" : data.margin.trend === "down" ? "down" : ""}>
                {data.margin.trend === "up" ? "s'élargit" : data.margin.trend === "down" ? "se resserre" : "stable"} ({signed(data.margin.change_pct_20)} % sur
                20 séances)
              </span>
              {data.margin.trend === "up" ? " : tension propre à la matière." : data.margin.trend === "flat" ? " : une hausse serait une poussée des coûts." : "."}
            </p>
          )}
        </section>

        <section className="panel">
          <p className="eyebrow">Analyse</p>
          {v ? (
            <>
              <p>
                <span className={verdictClass(v)}>{v.label}</span>{" "}
                {v.x_low !== null && v.x_high !== null && v.verdict !== "ras" && (
                  <span className="muted">
                    · {v.x_low.toFixed(0)} à {v.x_high.toFixed(0)} sem.
                  </span>
                )}
              </p>
              <p>
                <Pressure value={v.pressure} label={v.pressure_label} />
              </p>
              <p>{clip(v.explanation)}</p>
              {v.rules_only ? <p className="warn">Estimation sans IA (plafond de veille atteint).</p> : null}
            </>
          ) : (
            <p className="muted">Pas encore de verdict : il apparaît après le premier cycle de veille.</p>
          )}
          {data.analysis && (
            <>
              <p className="eyebrow">Dernière analyse de l'IA</p>
              <p>{clip(data.analysis.headline, 200)}</p>
              <p className="muted">{clip(data.analysis.summary)}</p>
              {data.analysis.triggers.length > 0 && (
                <>
                  <p className="eyebrow">Ce qui changerait l'avis</p>
                  <ul className="plain">
                    {data.analysis.triggers.slice(0, 3).map((t, i) => (
                      <li key={i}>
                        {clip(t.signal, 200)} <span className="muted">· {clip(t.implication, 200)}</span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
          <button className="tab" onClick={props.onReliability}>
            Fiabilité de cette matière
          </button>
        </section>
      </div>

      {data.upcoming.length > 0 && (
        <section className="panel band">
          <p className="eyebrow">Goulots en préparation</p>
          <ul className="plain inline">
            {data.upcoming.map((u, i) => (
              <li key={i}>
                <span className="mono">{u.date}</span> {clip(u.what, 120)} <span className="muted">({u.source})</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="sheet-grid">
        <section className="panel">
          <p className="eyebrow">Événements</p>
          {data.events.length === 0 ? (
            <p className="muted">Aucun événement sur la chaîne ces 60 derniers jours.</p>
          ) : (
            <ul className="plain events">
              {data.events.map((e) => (
                <li key={e.id}>
                  <button className={selected === e.id ? "event selected" : "event"} onClick={() => setSelected(e.id)}>
                    <span className={`letter ${e.status}`}>{e.letter}</span>
                    <span className="event-body">
                      <strong>{clip(e.title, 140)}</strong>
                      <span className="muted">
                        {clip(e.place, 60)} · {e.status_label} · {e.lineages} source{e.lineages > 1 ? "s" : ""} indépendante
                        {e.lineages > 1 ? "s" : ""}
                        {e.nodes.length > 0 ? ` · maillon : ${clip(e.nodes.join(", "), 80)}` : ""}
                      </span>
                      {e.impact && (
                        <span className="mono">
                          {signed(e.impact.p10)} à {signed(e.impact.p90)} {e.impact.unit === "percent" ? "%" : "unités"} sous 20 séances ·
                          probabilité {(e.impact.probability * 100).toFixed(0)} %
                        </span>
                      )}
                      {e.publishers.length > 0 && <span className="muted">{e.publishers.join(", ")}</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="panel">
          <EventMap events={data.events} sites={data.sites} selected={selected} onSelect={setSelected} />
        </section>
      </div>

      <Comparables eventId={selected ?? data.events.find((e) => e.status === "ongoing")?.id ?? data.events[0]?.id ?? null} onLibrary={props.onCases} />

      <section className="panel">
        <p className="eyebrow">Séries de prix liées</p>
        <ul className="plain">
          {data.links.map((l) => (
            <li key={`${l.instrument_id}-${l.role}`} className="row">
              <span>{clip(l.name, 80)}</span>
              <span className="muted">
                {l.role === "factor" ? "facteur de marché" : RANK_LABEL[l.rank]} · {l.validated ? "validée" : "proposée, à valider"}
              </span>
              {!l.validated && <button onClick={() => validate(l.instrument_id)}>Valider</button>}
            </li>
          ))}
        </ul>
        <div className="form inline-form">
          <select value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} aria-label="Source">
            {SOURCES.map(([k, label]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
          <input value={form.code} maxLength={80} placeholder="Code de la série" aria-label="Code de la série" onChange={(e) => setForm({ ...form, code: e.target.value })} />
          <input value={form.name} maxLength={120} placeholder="Nom affiché" aria-label="Nom affiché" onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} aria-label="Rôle">
            {ROLES.map(([k, label]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
          <button className="primary" disabled={!form.code.trim() || !form.name.trim()} onClick={addLink}>
            Ajouter
          </button>
        </div>
      </section>
    </div>
  );
}
