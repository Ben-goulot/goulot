import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { clip } from "../chain/types";

type Budget = {
  mode: "normal" | "economy" | "crisis_reserve" | "rules_only";
  spent_usd: number;
  cap_usd: number;
  reconstruction_spent_usd: number;
  reconstruction_cap_usd: number;
  serious_event: boolean;
  raise_headroom_usd: number;
};
type DeliveryZone = { zone: string; choices: Record<string, string> };
type SentAlert = { kind: string; text: string; created_at: string; sent_at: string | null; error: string };

const MODE: Record<Budget["mode"], string> = {
  normal: "normal",
  economy: "économie (80 %) : seuls les événements corroborés sont analysés",
  crisis_reserve: "réserve de crise (90 %) : seuls les événements graves sont analysés",
  rules_only: "règles seules (100 %) : la veille continue sans IA",
};
const usd = (v: number) => `${v.toFixed(2).replace(".", ",")} $`;

export function WatchSettings() {
  const [budget, setBudget] = useState<Budget | null>(null);
  const [alerts, setAlerts] = useState<SentAlert[]>([]);
  const [extra, setExtra] = useState("1");
  const [message, setMessage] = useState<string | null>(null);
  const [zone, setZone] = useState<DeliveryZone | null>(null);

  const load = useCallback(async () => {
    try {
      setBudget(await api.get<Budget>("/api/budget"));
      setAlerts(await api.get<SentAlert[]>("/api/alerts"));
      setZone(await api.get<DeliveryZone>("/api/settings/delivery-zone"));
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setMessage(null);
    try {
      await fn();
      setMessage(done);
      await load();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };

  return (
    <section className="panel">
      <h2>Veille et alertes</h2>
      {message && <p className="banner">{message}</p>}
      {budget && (
        <>
          <h3>Budget IA du mois</h3>
          <ul className="plain">
            <li>
              Veille : <span className="mono">{usd(budget.spent_usd)}</span> sur <span className="mono">{usd(budget.cap_usd)}</span> · mode{" "}
              {MODE[budget.mode]}
            </li>
            <li>
              Reconstructions : <span className="mono">{usd(budget.reconstruction_spent_usd)}</span> sur{" "}
              <span className="mono">{usd(budget.reconstruction_cap_usd)}</span>
            </li>
          </ul>
          {budget.serious_event && budget.raise_headroom_usd > 0 && (
            <div className="row">
              <span>Un événement grave est en cours : tu peux relever le plafond de veille de ce mois.</span>
              <input value={extra} inputMode="decimal" aria-label="Montant en dollars" onChange={(e) => setExtra(e.target.value)} />
              <button
                className="primary"
                onClick={() => act(() => api.post("/api/budget/raise", { extra_usd: Number(extra.replace(",", ".")) }), "Plafond relevé pour ce mois.")}
              >
                Relever (au plus {usd(budget.raise_headroom_usd)})
              </button>
            </div>
          )}
        </>
      )}
      <h3>Alertes</h3>
      <div className="row">
        <button onClick={() => act(() => api.post("/api/alerts/test"), "Message de test envoyé.")}>Envoyer un message de test</button>
        <button onClick={() => act(() => api.put("/api/settings/alerts-discreet", { enabled: true }), "Alertes discrètes activées.")}>
          Alertes discrètes
        </button>
        <button onClick={() => act(() => api.put("/api/settings/alerts-discreet", { enabled: false }), "Alertes détaillées activées.")}>
          Alertes détaillées
        </button>
      </div>
      <p className="muted">
        Discrètes : « Goulot : un verdict a changé, ouvre l'app », sans nom de matière. Aucune alerte ne contient de lien ni de donnée
        privée.
      </p>
      {zone && (
        <>
          <h3>Zone de livraison</h3>
          <label>
            Les flux maritimes vers toi passent par les détroits de cette route
            <select
              value={zone.zone}
              onChange={(e) =>
                act(() => api.put("/api/settings/delivery-zone", { zone: e.target.value }), "Zone de livraison enregistrée : routes recalculées.")
              }
            >
              {Object.entries(zone.choices).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <p className="muted">
            Goulot ajoute à tes chaînes les détroits probables (Ormuz, Malacca, Suez…) et les surveille. Ce réglage n'indique qu'une
            grande région ; il apparaît dans la chaîne type, donc dans ce que lit l'IA.
          </p>
        </>
      )}
      <h3>Alerte de vie externe (facultative)</h3>
      <div className="row">
        <button onClick={() => act(() => api.put("/api/settings/heartbeat", { enabled: true }), "Alerte de vie activée.")}>Activer</button>
        <button onClick={() => act(() => api.put("/api/settings/heartbeat", { enabled: false }), "Alerte de vie désactivée.")}>
          Désactiver
        </button>
      </div>
      <p className="muted">
        Un Mac éteint ne prévient personne : un service de surveillance gratuit te prévient s'il ne reçoit plus d'appel pendant 24 heures.
        L'adresse secrète se saisit dans « ./bin/goulot setup ».
      </p>
      {alerts.length > 0 && (
        <>
          <h3>Dernières alertes</h3>
          <ul className="plain">
            {alerts.slice(0, 10).map((a, i) => (
              <li key={i}>
                <span className="mono muted">{a.created_at.slice(0, 16).replace("T", " ")}</span> {clip(a.text, 300)}{" "}
                <span className={a.sent_at ? "muted" : "warn"}>{a.sent_at ? "· envoyée" : `· non envoyée${a.error ? ` (${a.error})` : ""}`}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
