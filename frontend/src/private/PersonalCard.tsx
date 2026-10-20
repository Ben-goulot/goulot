import { useState } from "react";
import { api, ApiError } from "../api";
import { clip } from "../chain/types";
import type { LockedPersonal } from "../watch/types";
import { useMyChain } from "./MyChainSide";
import { Locked, Pv, usePrivacy } from "./privacy";
import { SIGNAL_LABELS, fmtPct, fmtWeeks, type Personal } from "./types";

const today = () => new Date().toISOString().slice(0, 10);

function windowText(w: number | null | undefined): string {
  if (w === null || w === undefined) return "";
  return w < 0 ? "fenêtre d'action dépassée" : `fenêtre d'action ${fmtWeeks(w)}`;
}

/** The personal verdict on the material sheet. Vault closed: its label and window, « motif dans le coffre ». */
export function PersonalCard(props: { materialId: string; locked: LockedPersonal | null; onChain: () => void }) {
  const { vaultOpen } = usePrivacy();
  if (!vaultOpen) {
    return (
      <section className="panel">
        <p className="eyebrow">Ton approvisionnement</p>
        {props.locked && props.locked.verdict !== "ras" ? (
          <p>
            <span className={`badge ${props.locked.verdict}`}>{props.locked.label}</span>{" "}
            <span className="muted">
              {windowText(props.locked.window_weeks)} · {props.locked.reason}
            </span>
          </p>
        ) : null}
        <Locked text="Verdict personnel, signaux fournisseurs, devis et achats : dans le coffre." />
      </section>
    );
  }
  return <OpenCard {...props} />;
}

function OpenCard(props: { materialId: string; onChain: () => void }) {
  const my = useMyChain(props.materialId);
  const [signal, setSignal] = useState<Record<string, string> | null>(null);
  const [price, setPrice] = useState<{ kind: "quotes" | "purchases"; date: string; unit_price: string; currency: string; unit: string; quantity: string } | null>(null);
  const [cover, setCover] = useState<Record<string, string> | null>(null);
  const p: Personal | null = my.personal;
  const sites = my.data?.chain.sites ?? [];
  const base = `/api/private/materials/${props.materialId}`;

  const run = async (fn: () => Promise<unknown>, done: string) => {
    try {
      await fn();
      my.setMessage(done);
      await my.load();
    } catch (e) {
      my.setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };
  const n = (v: string | undefined) => (v === undefined || v.trim() === "" ? null : Number(v.replace(",", ".")));

  if (!p) return <section className="panel">{my.message ?? "Chargement du coffre…"}</section>;
  const v = p.verdict;

  return (
    <section className="panel personal">
      <div className="row spread">
        <p className="eyebrow">Ton approvisionnement · coffre ouvert</p>
        <button className="tab" onClick={props.onChain}>
          Ma chaîne
        </button>
      </div>
      {my.message && <p className="muted">{my.message}</p>}
      {v ? (
        <>
          <p>
            <span className={`badge ${v.verdict}`}>{v.label ?? v.verdict}</span>{" "}
            <span className="muted">
              {v.x_low !== null && v.x_low !== undefined && v.verdict !== "ras" ? (
                <Pv>
                  X {v.x_low === v.x_high ? fmtWeeks(v.x_low) : `${fmtWeeks(v.x_low)} à ${fmtWeeks(v.x_high)}`} · {windowText(v.window_weeks)}
                </Pv>
              ) : null}
            </span>
          </p>
          <p>
            <Pv>{clip(v.explanation ?? "", 800)}</Pv>
          </p>
          {Object.entries(v.exposures ?? {})
            .filter(([, e]) => e.on_path)
            .slice(0, 5)
            .map(([id, e]) => (
              <p key={id} className="muted small">
                <Pv>
                  {clip(e.title, 100)} : {fmtPct(e.exposure_pct)} de ton volume · sévérité marché {e.s}, pour toi {e.s_perso}
                </Pv>
              </p>
            ))}
          {(v.plan_b ?? []).length > 0 && (
            <p>
              Plan B :{" "}
              <Pv>
                {(v.plan_b ?? []).map((b) => `${b.name} (${b.status}${b.switch_weeks !== null ? `, bascule en ${b.switch_weeks} sem.` : ""})`).join(" ; ")}
              </Pv>
            </p>
          )}
        </>
      ) : (
        <p className="muted">Pas encore de verdict personnel : déclare Ma chaîne, ta couverture ou un signal fournisseur.</p>
      )}

      {p.questions.length > 0 && (
        <div className="card">
          <p className="eyebrow">À l'échéance</p>
          {p.questions.map((q) => (
            <div key={q.seq} className="row">
              <span>
                Conseil du {q.at.slice(0, 10)} (« {q.label} ») : as-tu constaté une tension chez ton fournisseur ?
              </span>
              <button onClick={() => run(() => api.post("/api/private/feedback", { seq: q.seq, answer: "tension" }), "Réponse inscrite au registre.")}>
                Oui
              </button>
              <button onClick={() => run(() => api.post("/api/private/feedback", { seq: q.seq, answer: "none" }), "Réponse inscrite au registre.")}>
                Rien constaté
              </button>
            </div>
          ))}
        </div>
      )}

      {p.private_events.length > 0 && (
        <>
          <p className="eyebrow">Événements qui ne touchent que Ma chaîne</p>
          <ul className="plain">
            {p.private_events.map((e) => (
              <li key={e.id}>
                {clip(e.title, 140)} <span className="muted">· {clip(e.place, 60)} · {e.confirmation === "confirmed" ? "confirmé" : "signal précoce"}</span>{" "}
                <Pv>
                  <span className="muted">→ {(p.links[e.id]?.site_names ?? []).join(", ")}</span>
                </Pv>
              </li>
            ))}
          </ul>
        </>
      )}

      <p className="eyebrow">Signaux fournisseurs</p>
      {p.signals.length === 0 && <p className="muted">Aucun signal déclaré.</p>}
      <ul className="plain">
        {p.signals.map((s) => (
          <li key={s.id} className="row">
            <Pv>
              {s.label} · {s.site || "fournisseur non précisé"} · {s.date}
              {s.pct !== null ? ` · ${s.pct} %` : ""}
              {s.effective_date ? ` au ${s.effective_date}` : ""}
              {s.new_lead_days !== null ? ` · délai ${s.old_lead_days ?? "?"} → ${s.new_lead_days} j` : ""}
            </Pv>
            <span className={s.expired ? "warn" : "muted"}>{s.expired ? "expiré : à confirmer" : s.active ? "en vigueur" : "clos"}</span>
            {s.type !== "back_to_normal" && (s.active || s.expired) && (
              <>
                <button onClick={() => run(() => api.post(`${base}/signals`, { type: s.type, site_id: s.site_id, date: today(), effective_date: s.effective_date, pct: s.pct, old_lead_days: s.old_lead_days, new_lead_days: s.new_lead_days, comment: s.comment }), "Signal confirmé pour 90 jours.")}>
                  Confirmer
                </button>
                <button onClick={() => run(() => api.post(`${base}/signals`, { type: "back_to_normal", site_id: s.site_id, date: today() }), "Retour à la normale noté.")}>
                  Retour à la normale
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
      {signal ? (
        <div className="form">
          <label>
            Type
            <select value={signal.type} onChange={(e) => setSignal({ ...signal, type: e.target.value })}>
              {Object.entries(SIGNAL_LABELS).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Site concerné
            <select value={signal.site_id} onChange={(e) => setSignal({ ...signal, site_id: e.target.value })}>
              <option value="">non précisé</option>
              {sites.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Date
            <input type="date" value={signal.date} onChange={(e) => setSignal({ ...signal, date: e.target.value })} />
          </label>
          {signal.type === "price_increase" && (
            <>
              <label>
                Hausse (%)
                <input inputMode="decimal" value={signal.pct ?? ""} onChange={(e) => setSignal({ ...signal, pct: e.target.value })} />
              </label>
              <label>
                Date d'effet
                <input type="date" value={signal.effective_date ?? ""} onChange={(e) => setSignal({ ...signal, effective_date: e.target.value })} />
              </label>
            </>
          )}
          {signal.type === "delay" && (
            <>
              <label>
                Ancien délai (jours)
                <input inputMode="decimal" value={signal.old ?? ""} onChange={(e) => setSignal({ ...signal, old: e.target.value })} />
              </label>
              <label>
                Nouveau délai (jours)
                <input inputMode="decimal" value={signal.new ?? ""} onChange={(e) => setSignal({ ...signal, new: e.target.value })} />
              </label>
            </>
          )}
          <label>
            Commentaire
            <input value={signal.comment ?? ""} maxLength={500} onChange={(e) => setSignal({ ...signal, comment: e.target.value })} />
          </label>
          <div className="row">
            <button
              className="primary"
              onClick={() =>
                run(async () => {
                  await api.post(`${base}/signals`, {
                    type: signal.type,
                    site_id: signal.site_id || null,
                    date: signal.date,
                    effective_date: signal.effective_date || null,
                    pct: n(signal.pct),
                    old_lead_days: n(signal.old),
                    new_lead_days: n(signal.new),
                    comment: signal.comment ?? "",
                  });
                  setSignal(null);
                }, "Signal enregistré dans le coffre et inscrit au registre.")
              }
            >
              Enregistrer dans le coffre
            </button>
            <button onClick={() => setSignal(null)}>Annuler</button>
          </div>
          <p className="muted small">Un signal compte comme une source fiable, sans corroboration, et expire après 90 jours sans confirmation.</p>
        </div>
      ) : (
        <button className="primary" onClick={() => setSignal({ type: "delay", site_id: "", date: today() })}>
          Signal fournisseur
        </button>
      )}

      <p className="eyebrow">Ma couverture</p>
      {cover ? (
        <div className="form inline-form">
          <label>
            Stock (semaines)
            <input inputMode="decimal" value={cover.stock ?? ""} onChange={(e) => setCover({ ...cover, stock: e.target.value })} />
          </label>
          <label>
            Délai fournisseur (jours)
            <input inputMode="decimal" value={cover.lead ?? ""} onChange={(e) => setCover({ ...cover, lead: e.target.value })} />
          </label>
          <label>
            Seuil de hausse (%)
            <input inputMode="decimal" value={cover.thr ?? ""} onChange={(e) => setCover({ ...cover, thr: e.target.value })} />
          </label>
          <button
            className="primary"
            onClick={() =>
              run(async () => {
                const lead = n(cover.lead);
                await api.put(`/api/private/coverage/${props.materialId}`, {
                  stock_weeks: n(cover.stock),
                  supplier_lead_time_days: lead === null ? null : Math.round(lead),
                  price_threshold_pct: n(cover.thr),
                });
                setCover(null);
              }, "Couverture enregistrée dans le coffre.")
            }
          >
            Enregistrer
          </button>
        </div>
      ) : (
        <p>
          <Pv>
            Stock {p.coverage?.stock_weeks ?? "—"} sem. · délai fournisseur {p.coverage?.supplier_lead_time_days ?? "—"} j · seuil {p.coverage?.price_threshold_pct ?? 5} %
          </Pv>{" "}
          <button
            onClick={() =>
              setCover({
                stock: p.coverage?.stock_weeks?.toString() ?? "",
                lead: p.coverage?.supplier_lead_time_days?.toString() ?? "",
                thr: p.coverage?.price_threshold_pct?.toString() ?? "",
              })
            }
          >
            Modifier
          </button>
        </p>
      )}

      <p className="eyebrow">Devis et achats (série privée)</p>
      <table className="watchlist">
        <tbody>
          {[...p.quotes.map((q) => ({ ...q, kind: "quotes" as const })), ...p.purchases.map((q) => ({ ...q, kind: "purchases" as const }))]
            .sort((a, b) => b.date.localeCompare(a.date))
            .map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.date}</td>
                <td>{r.kind === "quotes" ? "Devis" : "Achat"}</td>
                <td className="mono">
                  <Pv>
                    {r.unit_price.toLocaleString("fr-FR")} {r.currency}/{r.unit}
                    {r.quantity ? ` · ${r.quantity.toLocaleString("fr-FR")} ${r.unit}` : ""}
                  </Pv>
                </td>
                <td>
                  <button onClick={() => run(() => api.del(`${base}/${r.kind}/${r.id}`), "Supprimé.")}>Supprimer</button>
                </td>
              </tr>
            ))}
        </tbody>
      </table>
      {price ? (
        <div className="form inline-form">
          <select value={price.kind} aria-label="Type" onChange={(e) => setPrice({ ...price, kind: e.target.value as "quotes" | "purchases" })}>
            <option value="quotes">Devis</option>
            <option value="purchases">Achat</option>
          </select>
          <input type="date" value={price.date} aria-label="Date" onChange={(e) => setPrice({ ...price, date: e.target.value })} />
          <input inputMode="decimal" value={price.unit_price} placeholder="Prix unitaire" aria-label="Prix unitaire" onChange={(e) => setPrice({ ...price, unit_price: e.target.value })} />
          <input value={price.currency} maxLength={3} aria-label="Devise" onChange={(e) => setPrice({ ...price, currency: e.target.value.toUpperCase() })} />
          <input value={price.unit} maxLength={12} aria-label="Unité" onChange={(e) => setPrice({ ...price, unit: e.target.value })} />
          {price.kind === "purchases" && <input inputMode="decimal" value={price.quantity} placeholder="Quantité" aria-label="Quantité" onChange={(e) => setPrice({ ...price, quantity: e.target.value })} />}
          <button
            className="primary"
            disabled={!n(price.unit_price)}
            onClick={() =>
              run(async () => {
                await api.post(`${base}/${price.kind}`, { date: price.date, unit_price: n(price.unit_price), currency: price.currency, unit: price.unit, quantity: n(price.quantity) });
                setPrice(null);
              }, "Enregistré dans le coffre.")
            }
          >
            Enregistrer
          </button>
          <button onClick={() => setPrice(null)}>Annuler</button>
        </div>
      ) : (
        <button onClick={() => setPrice({ kind: "quotes", date: today(), unit_price: "", currency: "EUR", unit: "t", quantity: "" })}>Ajouter un devis ou un achat</button>
      )}
    </section>
  );
}
