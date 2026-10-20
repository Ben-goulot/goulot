import { MarkerType, type Edge, type Node } from "@xyflow/react";
import { Fragment, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { clip, type Chain, type ChainNode } from "../chain/types";
import { COL_WIDTH, ROW_HEIGHT } from "../chain/layout";
import { Pv, usePrivacy } from "./privacy";
import { SiteForm, type SiteDraft } from "./SiteForm";
import { fmtPct, fmtWeeks, newSiteId, type Described, type MyChain, type MyChainResponse, type Personal, type Site } from "./types";

const LOGISTICS = new Set(["port", "chokepoint", "waterway", "distributor"]);
const PRIVATE_PREFIX = "site:";

/** Ma chaîne, loaded only while the vault is open and dropped when it closes. */
export function useMyChain(materialId: string) {
  const { vaultOpen } = usePrivacy();
  const [data, setData] = useState<MyChainResponse | null>(null);
  const [personal, setPersonal] = useState<Personal | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!vaultOpen) return;
    try {
      setData(await api.get<MyChainResponse>(`/api/private/materials/${materialId}/my-chain`));
      setPersonal(await api.get<Personal>(`/api/private/materials/${materialId}/personal`));
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  }, [materialId, vaultOpen]);

  useEffect(() => {
    if (vaultOpen) void load();
    else {
      setData(null); // vault closed: private data leave the page's memory
      setPersonal(null);
    }
  }, [vaultOpen, load]);

  const save = async (chain: MyChain): Promise<boolean> => {
    try {
      const r = await api.put<{ revision: number; errors: string[]; warnings: string[] }>(`/api/private/materials/${materialId}/my-chain`, chain);
      if (!r.revision) {
        setMessage(`Non enregistré : ${r.errors.join(" ")}`);
        return false;
      }
      setMessage(`Révision ${r.revision} enregistrée, chiffrée sur ce Mac.`);
      await load();
      return true;
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
      return false;
    }
  };

  return { data, personal, message, setMessage, save, load };
}

function siteNodeId(chain: MyChain, siteId: string): string {
  const site = chain.sites.find((s) => s.id === siteId);
  return site?.node_key ? site.node_key : `${PRIVATE_PREFIX}${siteId}`;
}

function widthClass(flow: number): string {
  return flow >= 60 ? "w5" : flow >= 40 ? "w4" : flow >= 20 ? "w3" : flow >= 10 ? "w2" : "w1";
}

/** Ma chaîne over the typical chain: private sites as dashed blue nodes, links in blue with a thickness by share of
 * volume, adopted nodes outlined, unused production nodes faded with « pas chez toi ». */
export function overlay(chain: Chain, nodes: Node[], edges: Edge[], my: MyChainResponse | null, selected: string | null, presenting: boolean) {
  if (!my) return { nodes, edges };
  const doc = my.chain;
  const sink = chain.nodes.find((n) => n.tier === 0)?.node_key ?? "";
  const adopted = new Set(doc.sites.map((s) => s.node_key).filter((k): k is string => Boolean(k)));
  const notUsed = new Set(doc.not_used);
  const producers = new Set(chain.nodes.filter((n) => n.tier === 1 && n.node_type !== "material" && !LOGISTICS.has(n.node_type)).map((n) => n.node_key));
  const used = doc.sites.length > 0;
  const styled = nodes.map((n) => {
    if (n.id.startsWith("col:")) return n;
    const faded = notUsed.has(n.id) || (used && producers.has(n.id) && !adopted.has(n.id));
    const cls = `${n.className ?? ""}${adopted.has(n.id) ? " adopted" : ""}${faded ? " faded" : ""}`;
    return { ...n, className: cls };
  });
  const xs = (pred: (c: ChainNode) => boolean) => {
    const found = nodes.find((n) => chain.nodes.some((c) => c.node_key === n.id && pred(c)));
    return found?.position.x;
  };
  const prodX = xs((c) => producers.has(c.node_key)) ?? 3 * COL_WIDTH;
  const logX = xs((c) => c.tier === 1 && LOGISTICS.has(c.node_type)) ?? prodX + COL_WIDTH;
  const bottom = (x: number) => Math.max(0, ...nodes.filter((n) => n.position.x === x).map((n) => n.position.y));
  const counters = new Map<number, number>();
  const extra: Node[] = doc.sites
    .filter((s) => !s.node_key)
    .map((s) => {
      const x = s.role === "producer" || s.role === "processor" ? prodX : logX;
      const k = (counters.get(x) ?? 0) + 1;
      counters.set(x, k);
      const exposure = my.sites?.[s.id]?.exposure_pct;
      return {
        id: `${PRIVATE_PREFIX}${s.id}`,
        position: { x, y: bottom(x) + k * ROW_HEIGHT },
        data: {
          label: (
            <div>
              <div className="node-name">
                <Pv>{clip(s.name, 60)}</Pv>
              </div>
              <div className="node-sub">
                privé · {my.roles[s.role] ?? s.role}
                {exposure !== undefined && (
                  <>
                    {" · "}
                    <Pv>{fmtPct(exposure)}</Pv>
                  </>
                )}
              </div>
            </div>
          ),
        },
        className: `chain-node private-site${selected === `${PRIVATE_PREFIX}${s.id}` ? " selected" : ""}`,
        sourcePosition: "right" as Node["sourcePosition"],
        targetPosition: "left" as Node["targetPosition"],
        draggable: false,
      };
    });
  const flows = new Map((my.flows ?? []).map((f) => [`${f.source}>${f.target}`, f.flow_pct]));
  const myEdges: Edge[] = doc.links.map((l, i) => {
    const flow = flows.get(`${l.source}>${l.target}`) ?? 0;
    return {
      id: `my:${i}`,
      source: siteNodeId(doc, l.source),
      target: l.target === "me" ? sink : siteNodeId(doc, l.target),
      label: presenting ? undefined : `${flow.toLocaleString("fr-FR", { maximumFractionDigits: 0 })} %`,
      className: `my-edge ${widthClass(flow)}`,
      markerEnd: { type: MarkerType.ArrowClosed, color: "#6CB2FF" },
    };
  });
  return { nodes: [...styled, ...extra], edges: [...edges.map((e) => ({ ...e, className: `${e.className ?? ""} dim` })), ...myEdges] };
}

function SiteFacts(props: { site: Site; my: MyChainResponse; personal: Personal | null }) {
  const { site, my } = props;
  const info = my.sites?.[site.id];
  const out = my.chain.links.filter((l) => l.source === site.id);
  const names = new Map(my.chain.sites.map((s) => [s.id, s.name]));
  const touching = Object.entries(props.personal?.links ?? {}).filter(([, v]) => v.sites.includes(site.id));
  return (
    <dl className="facts">
      <dt>Rôle</dt>
      <dd>
        {my.roles[site.role] ?? site.role}
        {site.node_key ? " · maillon de la chaîne type" : " · absent de la chaîne type"}
      </dd>
      <dt>Part de ton volume</dt>
      <dd>
        <Pv>{fmtPct(info?.exposure_pct)}</Pv>
        {info?.weighs ? " · pèse pour toi : déclenche l'analyse" : ""}
      </dd>
      {out.map((l, i) => (
        <Fragment key={i}>
          <dt>Fournit</dt>
          <dd>
            <Pv>
              {l.target === "me" ? "toi" : names.get(l.target)} · {fmtPct(l.share_pct)} · {l.lead_days !== null ? `${l.lead_days} j` : "délai inconnu"}
              {l.incoterm ? ` · ${l.incoterm}` : ""}
            </Pv>
          </dd>
        </Fragment>
      ))}
      <dt>Stock chez lui</dt>
      <dd>
        <Pv>{site.stock_days !== null ? `${site.stock_days} j` : "non communiqué"}</Pv>
      </dd>
      <dt>Amont</dt>
      <dd>
        {info && info.unexplained_pct > 0
          ? `${fmtPct(info.unexplained_pct)} non déclaré : ${site.node_key ? "comme la chaîne type" : "supposé comme le marché"}`
          : "déclaré par toi"}
      </dd>
      <dt>Lieu</dt>
      <dd>
        <Pv>{site.city || "ville non précisée"}</Pv> {site.country_iso2 ? `(${site.country_iso2})` : ""}
        {site.lat === null ? " · pas de point sur la carte" : ""}
      </dd>
      <dt>Surveillance</dt>
      <dd>{my.watch_levels[site.watch_level]}</dd>
      <dt>Plan B</dt>
      <dd>
        {site.alternatives.length === 0
          ? "aucune alternative"
          : site.alternatives.map((a, i) => (
              <span key={i}>
                <Pv>{a.name}</Pv> ({a.status === "qualified" ? "qualifiée" : a.status === "qualifying" ? "en cours" : "non qualifiée"}
                {a.switch_weeks !== null ? `, bascule en ${a.switch_weeks} sem.` : ""}){" "}
              </span>
            ))}
      </dd>
      {touching.length > 0 && (
        <>
          <dt>Événements</dt>
          <dd>{touching.length} en cours sur ce site (voir la fiche matière)</dd>
        </>
      )}
    </dl>
  );
}

function blankSite(chain: MyChain, name: string, node: ChainNode | null): SiteDraft {
  const id = newSiteId(chain, name || "site");
  const site: Site = {
    id,
    name,
    role: node && LOGISTICS.has(node.node_type) ? "distributor" : "producer",
    company: "",
    aliases: [],
    city: "",
    country_iso2: node && node.country_iso2 !== "unknown" ? node.country_iso2 : "",
    lat: null,
    lon: null,
    node_key: node ? node.node_key : null,
    watch_level: "city_material",
    stock_days: null,
    alternatives: [],
    notes: "",
  };
  return { site, links: [{ source: id, target: "me", share_pct: 100, transport_mode: "unknown", incoterm: "", lead_days: null }] };
}

export function MyChainSide(props: {
  materialId: string;
  chain: Chain;
  selected: string | null;
  my: ReturnType<typeof useMyChain>;
  onSelect: (id: string | null) => void;
}) {
  const { data, personal, save, setMessage } = props.my;
  const [editing, setEditing] = useState<SiteDraft | null>(null);
  const [quick, setQuick] = useState<{ share: string; weeks: string } | null>(null);
  const [preview, setPreview] = useState<(Described & { chain: MyChain | null; errors: string[] }) | null>(null);

  useEffect(() => {
    setEditing(null);
    setQuick(null);
  }, [props.selected]);

  if (!data) return <p className="muted">Chargement de Ma chaîne…</p>;
  const doc = data.chain;
  const siteId = props.selected?.startsWith(PRIVATE_PREFIX)
    ? props.selected.slice(PRIVATE_PREFIX.length)
    : doc.sites.find((s) => s.node_key && s.node_key === props.selected)?.id ?? null;
  const site = doc.sites.find((s) => s.id === siteId) ?? null;
  const node = props.chain.nodes.find((n) => n.node_key === props.selected) ?? null;

  const commit = async (d: SiteDraft) => {
    const others = doc.links.filter((l) => l.source !== d.site.id);
    const sites = doc.sites.some((s) => s.id === d.site.id) ? doc.sites.map((s) => (s.id === d.site.id ? d.site : s)) : [...doc.sites, d.site];
    if (await save({ ...doc, sites, links: [...others, ...d.links.map((l) => ({ ...l, source: d.site.id }))] })) setEditing(null);
  };

  const remove = async (id: string) => {
    await save({ ...doc, sites: doc.sites.filter((s) => s.id !== id), links: doc.links.filter((l) => l.source !== id && l.target !== id) });
    props.onSelect(null);
  };

  const toggleUnused = async (key: string) => {
    const not_used = doc.not_used.includes(key) ? doc.not_used.filter((k) => k !== key) : [...doc.not_used, key];
    await save({ ...doc, not_used });
  };

  const readCsv = async (file: File) => {
    if (file.size > 1_000_000) {
      setMessage("Fichier trop gros : 1 Mo au plus.");
      return;
    }
    const text = await file.text(); // read by the page, sent as text: never stored as a file
    try {
      setPreview(await api.post(`/api/private/materials/${props.materialId}/my-chain/import`, { csv: text }));
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };

  if (editing) return <SiteForm chain={doc} draft={editing} onSave={commit} onCancel={() => setEditing(null)} />;

  if (site) {
    return (
      <>
        <p className="eyebrow">Site de Ma chaîne</p>
        <h3 className="title">
          <Pv>{clip(site.name, 120)}</Pv>
        </h3>
        <SiteFacts site={site} my={data} personal={personal} />
        <div className="row">
          <button onClick={() => setEditing({ site, links: doc.links.filter((l) => l.source === site.id) })}>Modifier</button>
          <button onClick={() => remove(site.id)}>Retirer de Ma chaîne</button>
        </div>
      </>
    );
  }

  if (node) {
    const excluded = doc.not_used.includes(node.node_key);
    return (
      <>
        <p className="eyebrow">Maillon de la chaîne type</p>
        <h3 className="title">{clip(node.name, 120)}</h3>
        <p className="muted">
          {node.global_share_pct !== null ? `${node.global_share_pct} % de l'offre mondiale · ` : ""}
          {excluded ? "tu as indiqué ne pas y passer." : "tu n'y passes pas aujourd'hui."}
        </p>
        <p className="eyebrow">Tu passes par ce maillon ?</p>
        {quick ? (
          <div className="form">
            <p>Trois champs pour commencer</p>
            <label>
              Ta part de volume (%)
              <input inputMode="decimal" value={quick.share} onChange={(e) => setQuick({ ...quick, share: e.target.value })} />
            </label>
            <label>
              Délai jusqu'à toi (semaines)
              <input inputMode="decimal" value={quick.weeks} onChange={(e) => setQuick({ ...quick, weeks: e.target.value })} />
            </label>
            <div className="row">
              <button
                className="primary"
                onClick={() => {
                  const d = blankSite(doc, node.name, node);
                  const share = Number(quick.share.replace(",", ".")) || 0;
                  const weeks = Number(quick.weeks.replace(",", "."));
                  d.links[0] = { ...d.links[0], share_pct: share, lead_days: Number.isFinite(weeks) && quick.weeks.trim() ? weeks * 7 : null };
                  void commit(d);
                  setQuick(null);
                }}
              >
                Enregistrer dans le coffre
              </button>
              <button onClick={() => setQuick(null)}>Annuler</button>
            </div>
            <p className="muted small">Stocks, route et alternatives se complètent plus tard, si tu veux.</p>
          </div>
        ) : (
          <div className="stack">
            <button className="primary" onClick={() => setQuick({ share: "", weeks: "" })}>
              C'est mon fournisseur
            </button>
            <button onClick={() => toggleUnused(node.node_key)}>{excluded ? "J'y passe finalement" : "Je ne passe pas par là"}</button>
            <button onClick={() => setEditing(blankSite(doc, "", null))}>Ajouter un site absent</button>
          </div>
        )}
      </>
    );
  }

  const c = data.concentration;
  const v = personal?.verdict;
  return (
    <>
      <p className="eyebrow">Ma chaîne</p>
      <p className="muted">
        {data.revision ? `Révision ${data.revision} du ${new Date(data.revisions[data.revisions.length - 1].created_at).toLocaleDateString("fr-FR")}, chiffrée sur ce Mac.` : "Pas encore de Ma chaîne pour cette matière."}
      </p>
      {data.check.errors.length > 0 && (
        <ul className="plain warn">
          {data.check.errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {data.check.warnings.length > 0 && (
        <ul className="plain muted small">
          {data.check.warnings.map((w) => (
            <li key={w}>
              <Pv>{w}</Pv>
            </li>
          ))}
        </ul>
      )}
      {v && v.verdict !== "ras" && (
        <div className="card">
          <p className="eyebrow">Ce que Ma chaîne change</p>
          <p>
            <span className={`badge ${v.verdict}`}>{v.label}</span> <span className="muted">{v.window_weeks !== null && v.window_weeks !== undefined ? (v.window_weeks < 0 ? "fenêtre d'action dépassée" : `fenêtre ${fmtWeeks(v.window_weeks)}`) : ""}</span>
          </p>
          <p>
            <Pv>{clip(v.explanation ?? "", 600)}</Pv>
          </p>
          {(v.plan_b ?? []).length > 0 && (
            <p>
              Plan B :{" "}
              {(v.plan_b ?? []).map((p, i) => (
                <Pv key={i}>
                  {p.name} ({p.status}
                  {p.switch_weeks !== null ? `, bascule en ${p.switch_weeks} sem.` : ""}){" "}
                </Pv>
              ))}
            </p>
          )}
        </div>
      )}
      {c && (
        <div className="card">
          <p className="eyebrow">Concentration cachée</p>
          <p>
            Marché : {c.market.top_country ?? "—"} {fmtPct(c.market.top_share_pct)} · HHI {c.market.hhi?.toLocaleString("fr-FR") ?? "—"}
          </p>
          <p>
            Toi :{" "}
            <Pv>
              {c.mine.top_country ?? "—"} {fmtPct(c.mine.top_share_pct)} · HHI {c.mine.hhi?.toLocaleString("fr-FR") ?? "—"}
            </Pv>
          </p>
          {c.mine.hhi !== null && c.market.hhi !== null && c.mine.hhi > c.market.hhi && <p className="warn">Le marché est diversifié, toi non.</p>}
          <p className="muted small">Part du premier pays et indice de Herfindahl-Hirschman, calculés sur ton Mac.</p>
        </div>
      )}
      <p className="muted">Clique sur un maillon pour l'adopter, l'écarter ou ajouter un site absent.</p>
      <div className="stack">
        <button onClick={() => setEditing(blankSite(doc, "", null))}>Ajouter un site</button>
        <label className="file-button">
          Importer un CSV de ton ERP
          <input type="file" accept=".csv,text/csv" onChange={(e) => e.target.files?.[0] && void readCsv(e.target.files[0])} />
        </label>
        <a href="/api/my-chain/template.csv" download>
          Télécharger le modèle CSV
        </a>
      </div>
      {preview && (
        <div className="card">
          <p className="eyebrow">Aperçu de l'import</p>
          {preview.errors.length > 0 ? (
            <ul className="plain warn">
              {preview.errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          ) : (
            <>
              <p>
                {preview.chain?.sites.length} site(s), {preview.chain?.links.length} lien(s).
                {preview.check.errors.length ? ` À corriger : ${preview.check.errors.join(" ")}` : ""}
              </p>
              <div className="row">
                <button
                  className="primary"
                  disabled={!preview.chain || preview.check.errors.length > 0}
                  onClick={async () => {
                    if (preview.chain && (await save({ ...preview.chain, not_used: doc.not_used }))) setPreview(null);
                  }}
                >
                  Remplacer Ma chaîne par cet import
                </button>
                <button onClick={() => setPreview(null)}>Annuler</button>
              </div>
            </>
          )}
        </div>
      )}
      {data.revision > 0 && (
        <button
          className="danger"
          onClick={async () => {
            if (!window.confirm("Effacer Ma chaîne de cette matière, ses signaux, devis et achats ? C'est définitif.")) return;
            await api.del(`/api/private/materials/${props.materialId}/my-chain`);
            await props.my.load();
          }}
        >
          Effacer Ma chaîne de cette matière
        </button>
      )}
      <p className="muted small">{data.cities_available ? data.attribution : "Liste des villes pas encore installée : place tes sites sur la carte."}</p>
      <p className="muted small">Rien de ce que tu saisis ne part vers l'IA ni vers les sources : tout reste chiffré sur ce Mac.</p>
    </>
  );
}
