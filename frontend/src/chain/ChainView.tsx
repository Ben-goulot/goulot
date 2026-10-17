import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { COLUMNS, layout } from "./layout";
import { BASIS, clip, CONFIDENCE, MODE, NODE_TYPE, safeHref, weeks, type Chain, type ChainNode, type Source } from "./types";

type Draft = {
  name: string;
  country_iso2: string;
  place: string;
  global_share_pct: string;
  buffer_days: string;
  confidence: "high" | "medium" | "low";
  notes: string;
};

function toDraft(n: ChainNode): Draft {
  return {
    name: n.name,
    country_iso2: n.country_iso2,
    place: n.place,
    global_share_pct: n.global_share_pct === null ? "" : String(n.global_share_pct),
    buffer_days: n.buffer_days === null ? "" : String(n.buffer_days),
    confidence: n.confidence,
    notes: n.notes,
  };
}

function SourceLinks(props: { refs: string[]; sources: Map<string, Source> }) {
  const items = props.refs.map((r) => props.sources.get(r)).filter((s): s is Source => Boolean(s));
  if (items.length === 0) return <span className="muted">aucune</span>;
  return (
    <ul className="plain">
      {items.map((s) => {
        const href = safeHref(s.url);
        return (
          <li key={s.ref}>
            {href ? (
              <a href={href} target="_blank" rel="noopener noreferrer" className="source">
                {clip(s.title || s.publisher, 120)}
              </a>
            ) : (
              clip(s.title, 120)
            )}{" "}
            <span className="muted">
              · {s.publisher}
              {s.page_age ? ` · ${s.page_age}` : ""}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function ChainView(props: { materialId: string; onBack: () => void }) {
  const [chain, setChain] = useState<Chain | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [ack, setAck] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setChain(await api.get<Chain>(`/api/materials/${props.materialId}/chain`));
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  }, [props.materialId]);

  useEffect(() => {
    void load();
  }, [load]);

  const sources = useMemo(() => new Map((chain?.sources ?? []).map((s) => [s.ref, s])), [chain]);

  const flow = useMemo(() => {
    if (!chain) return { nodes: [] as Node[], edges: [] as Edge[] };
    const pos = layout(chain.nodes);
    const nodes: Node[] = chain.nodes.map((n) => ({
      id: n.node_key,
      position: pos.get(n.node_key) ?? { x: 0, y: 0 },
      data: {
        label: (
          <div>
            <div className="node-name">{clip(n.name, 60)}</div>
            <div className="node-sub">
              {NODE_TYPE[n.node_type] ?? n.node_type}
              {n.country_iso2 !== "unknown" ? ` · ${n.country_iso2}` : ""}
              {n.global_share_pct !== null ? ` · ${n.global_share_pct} %` : ""}
            </div>
            {n.confidence === "low" && <div className="node-flag">confiance faible</div>}
          </div>
        ),
      },
      className: `chain-node conf-${n.confidence}${n.tier === 0 ? " purchased" : ""}${selected === n.node_key ? " selected" : ""}`,
      sourcePosition: "right" as Node["sourcePosition"],
      targetPosition: "left" as Node["targetPosition"],
      draggable: false,
    }));
    const edges: Edge[] = chain.edges.map((e) => ({
      id: String(e.id),
      source: e.from_key,
      target: e.to_key,
      label: e.transport_mode !== "none" && e.lead_time_days ? `${MODE[e.transport_mode]} · ${weeks(e.lead_time_days)}` : undefined,
      className: `chain-edge conf-${e.confidence}`,
      markerEnd: { type: MarkerType.ArrowClosed },
    }));
    return { nodes, edges };
  }, [chain, selected]);

  if (!chain) return <section className="panel">{message ?? "Chargement…"}</section>;

  const node = chain.nodes.find((n) => n.node_key === selected) ?? null;
  const lowCount = chain.nodes.filter((n) => n.confidence === "low").length;
  const editable = chain.material.status === "review" || chain.material.status === "validated";

  const save = async () => {
    if (!node || !draft) return;
    const num = (v: string) => (v.trim() === "" ? null : Number(v.replace(",", ".")));
    try {
      await api.patch(`/api/materials/${props.materialId}/nodes/${node.node_key}`, {
        name: draft.name,
        country_iso2: draft.country_iso2.trim().toUpperCase() || "unknown",
        place: draft.place,
        global_share_pct: num(draft.global_share_pct),
        buffer_days: num(draft.buffer_days),
        confidence: draft.confidence,
        notes: draft.notes,
      });
      setDraft(null);
      setMessage("Maillon corrigé.");
      await load();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };

  const validate = async () => {
    try {
      await api.post(`/api/materials/${props.materialId}/validate`, { acknowledge_injection: ack });
      setMessage("Chaîne validée. La veille sur cette chaîne arrive en phase 3.");
      await load();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };

  return (
    <div className="chain-page">
      <header className="chain-head">
        <button className="tab" onClick={props.onBack}>
          ← Marchés
        </button>
        <h2>Chaîne d'approvisionnement : {chain.material.name}</h2>
        <span className="muted mono">CAS {chain.material.cas}</span>
        <span className="muted">
          · {chain.nodes.length} maillons · {lowCount} en confiance faible ·{" "}
          {chain.material.status === "validated" ? "validée" : "à relire"}
        </span>
      </header>
      {message && <p className="banner">{message}</p>}
      {chain.injection_suspected && (
        <p className="banner">
          Une page lue pendant la recherche contenait des consignes adressées à l'IA. Relis les sources avant de valider.
        </p>
      )}
      <div className="chain-layout">
        <div className="chain-graph">
          <div className="columns">
            {COLUMNS.map((c) => (
              <span key={c}>{c}</span>
            ))}
          </div>
          <ReactFlow
            nodes={flow.nodes}
            edges={flow.edges}
            onNodeClick={(_, n) => {
              setSelected(n.id);
              setDraft(null);
            }}
            nodesConnectable={false}
            fitView
            colorMode="dark"
          >
            <Background />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
        <aside className="panel side">
          {node ? (
            <>
              <p className="eyebrow">Maillon sélectionné</p>
              <h3 className="title">{clip(node.name, 120)}</h3>
              <dl className="facts">
                <dt>Rôle</dt>
                <dd>
                  {NODE_TYPE[node.node_type] ?? node.node_type} · {clip(node.product, 120)}
                </dd>
                <dt>Lieu</dt>
                <dd>
                  {node.place !== "unknown" ? clip(node.place, 120) : "non précisé"}
                  {node.country_iso2 !== "unknown" ? ` (${node.country_iso2})` : ""}
                </dd>
                {node.global_share_pct !== null && (
                  <>
                    <dt>Part mondiale</dt>
                    <dd className="mono">{node.global_share_pct} %</dd>
                  </>
                )}
                {node.capacity_text !== "unknown" && (
                  <>
                    <dt>Capacité</dt>
                    <dd>{clip(node.capacity_text, 120)}</dd>
                  </>
                )}
                <dt>Stock tampon</dt>
                <dd>
                  {node.buffer_days !== null ? weeks(node.buffer_days) : "—"}
                  {node.buffer_is_default ? " (valeur par défaut, hypothèse)" : ""}
                </dd>
                <dt>Délai jusqu'à toi</dt>
                <dd>
                  {node.delay
                    ? node.delay.low === node.delay.high
                      ? weeks(node.delay.days)
                      : `${weeks(node.delay.low)} à ${weeks(node.delay.high)}`
                    : "aucun chemin jusqu'à la matière"}
                </dd>
                <dt>Confiance</dt>
                <dd>
                  {CONFIDENCE[node.confidence]}
                  {node.user_modified ? " · corrigé par toi" : ""}
                </dd>
                {node.notes && (
                  <>
                    <dt>Notes</dt>
                    <dd>{clip(node.notes)}</dd>
                  </>
                )}
              </dl>
              {node.anomalies.length > 0 && (
                <ul className="plain warn">
                  {node.anomalies.map((a) => (
                    <li key={a}>{clip(a)}</li>
                  ))}
                </ul>
              )}
              <p className="eyebrow">Sources</p>
              <SourceLinks refs={node.source_refs} sources={sources} />
              {chain.edges
                .filter((e) => e.from_key === node.node_key)
                .map((e) => (
                  <p key={e.id} className="muted">
                    Vers {chain.nodes.find((n) => n.node_key === e.to_key)?.name ?? e.to_key} : {MODE[e.transport_mode]}
                    {e.lead_time_days !== null ? `, ${weeks(e.lead_time_days)} (${BASIS[e.lead_time_basis]})` : ""}
                  </p>
                ))}
              {editable && !draft && <button onClick={() => setDraft(toDraft(node))}>Corriger ce maillon</button>}
              {draft && (
                <div className="form">
                  <label>
                    Nom
                    <input value={draft.name} maxLength={200} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                  </label>
                  <label>
                    Pays (code ISO à 2 lettres)
                    <input value={draft.country_iso2} maxLength={7} onChange={(e) => setDraft({ ...draft, country_iso2: e.target.value })} />
                  </label>
                  <label>
                    Lieu
                    <input value={draft.place} maxLength={200} onChange={(e) => setDraft({ ...draft, place: e.target.value })} />
                  </label>
                  <label>
                    Part mondiale, en %
                    <input value={draft.global_share_pct} inputMode="decimal" onChange={(e) => setDraft({ ...draft, global_share_pct: e.target.value })} />
                  </label>
                  <label>
                    Stock tampon, en jours
                    <input value={draft.buffer_days} inputMode="decimal" onChange={(e) => setDraft({ ...draft, buffer_days: e.target.value })} />
                  </label>
                  <label>
                    Confiance
                    <select value={draft.confidence} onChange={(e) => setDraft({ ...draft, confidence: e.target.value as Draft["confidence"] })}>
                      <option value="high">Élevée</option>
                      <option value="medium">Moyenne</option>
                      <option value="low">Faible</option>
                    </select>
                  </label>
                  <label>
                    Notes
                    <input value={draft.notes} maxLength={1000} onChange={(e) => setDraft({ ...draft, notes: e.target.value })} />
                  </label>
                  <div className="row">
                    <button className="primary" onClick={save}>
                      Enregistrer
                    </button>
                    <button onClick={() => setDraft(null)}>Annuler</button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <>
              <p className="eyebrow">Relecture</p>
              <p>Clique sur un maillon pour voir son rôle, ses sources et son délai jusqu'à toi.</p>
              {chain.routes.length > 0 && (
                <>
                  <p className="eyebrow">Voies de production</p>
                  <ul className="plain">
                    {chain.routes.map((r) => (
                      <li key={r.route_key}>
                        {clip(r.name, 160)}
                        {r.global_share_pct !== null ? ` · ${r.global_share_pct} %` : ""}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {chain.risks.length > 0 && (
                <>
                  <p className="eyebrow">Risques structurels</p>
                  <ul className="plain">
                    {chain.risks.map((r, i) => (
                      <li key={i}>{clip(r.description)}</li>
                    ))}
                  </ul>
                </>
              )}
              {chain.dates.length > 0 && (
                <>
                  <p className="eyebrow">Échéances connues</p>
                  <ul className="plain">
                    {chain.dates.map((d, i) => (
                      <li key={i}>
                        <span className="mono">{d.date}</span> {clip(d.what, 200)}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {chain.open_questions.length > 0 && (
                <>
                  <p className="eyebrow">Questions ouvertes</p>
                  <ul className="plain muted">
                    {chain.open_questions.map((q, i) => (
                      <li key={i}>{clip(q)}</li>
                    ))}
                  </ul>
                </>
              )}
              {chain.material.hs_codes.length > 0 && (
                <p className="muted">
                  Codes douaniers proposés : {chain.material.hs_codes.map((h) => h.code).join(", ")} (à vérifier).
                </p>
              )}
              {chain.material.status === "review" && (
                <div className="validate">
                  {chain.injection_suspected && (
                    <label className="row">
                      <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                      J'ai relu les sources signalées
                    </label>
                  )}
                  <button className="primary" disabled={chain.injection_suspected && !ack} onClick={validate}>
                    Valider la chaîne
                  </button>
                  <p className="muted">Rien n'est surveillé avant la validation.</p>
                </div>
              )}
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
