import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";

type Mode = "auto" | "ask" | "off";
type Updates = {
  version: string;
  installed: boolean;
  mode: Mode;
  paused: boolean;
  available: string | null;
  last_check: string | null;
  last_result: string;
  last_install: { version?: string; at?: string; key_id?: string };
  versions: string[];
  rejected: string[];
};
type Item = { status: "ok" | "warn" | "fail"; label: string; detail: string };

const MODES: { mode: Mode; label: string; hint: string }[] = [
  { mode: "auto", label: "Automatique", hint: "Chaque nuit à 4 h 30 et à l'ouverture de session, une version que vous avez approuvée sur GitHub" },
  { mode: "ask", label: "Me demander", hint: "Une alerte annonce la version ; « goulot update --install » l'installe" },
  { mode: "off", label: "Désactivé", hint: "Aucune vérification" },
];
const MARK: Record<Item["status"], string> = { ok: "✅", warn: "🟠", fail: "❌" };
const day = (iso?: string | null) => (iso ? iso.slice(0, 16).replace("T", " ") : "jamais");

// Settings > « Mises à jour » and « Bilan sécurité » (docs/SECURITY.md, docs/UPDATES.md). Texts are shown as text.
export function SystemSettings() {
  const [updates, setUpdates] = useState<Updates | null>(null);
  const [report, setReport] = useState<Item[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setUpdates(await api.get<Updates>("/api/updates"));
      setReport((await api.get<{ items: Item[] }>("/api/security/report")).items);
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const choose = async (mode: Mode) => {
    setMessage(null);
    try {
      await api.put("/api/settings/update-mode", { mode });
      setMessage("Réglage des mises à jour enregistré.");
      await load();
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : "Erreur.");
    }
  };

  return (
    <section className="panel">
      <h2>Mises à jour</h2>
      {message && <p className="banner">{message}</p>}
      {updates && (
        <>
          <ul className="plain">
            <li>
              Version active : <span className="mono">{updates.version}</span>
              {!updates.installed && " (copie de travail, pas encore installée)"}
            </li>
            {updates.last_install.version && (
              <li>
                Dernière installation : <span className="mono">{updates.last_install.version}</span> le {day(updates.last_install.at)},
                signature vérifiée (clé <span className="mono">{updates.last_install.key_id}</span>)
              </li>
            )}
            <li>
              Dernière vérification : {day(updates.last_check)} {updates.last_result && `· ${updates.last_result}`}
            </li>
            {updates.available && <li className="warn">Version {updates.available} disponible : « goulot update --install ».</li>}
            {updates.paused && <li className="warn">En pause après deux échecs : « goulot update --resume » une fois le motif vérifié.</li>}
            {updates.versions.length > 0 && <li>Versions gardées pour un retour arrière : {updates.versions.join(", ")}</li>}
            {updates.rejected.length > 0 && <li>Versions rejetées, jamais réinstallées : {updates.rejected.join(", ")}</li>}
          </ul>
          <div className="row" role="radiogroup" aria-label="Mode des mises à jour">
            {MODES.map((m) => (
              <button key={m.mode} className={updates.mode === m.mode ? "primary" : ""} aria-pressed={updates.mode === m.mode} onClick={() => void choose(m.mode)}>
                {m.label}
              </button>
            ))}
          </div>
          <p className="muted">{MODES.find((m) => m.mode === updates.mode)?.hint}. Retour à la version précédente : « goulot update --rollback ».</p>
        </>
      )}
      <h2>Bilan sécurité</h2>
      {report === null ? (
        <p className="muted">Chargement…</p>
      ) : (
        <ul className="plain">
          {report.map((item) => (
            <li key={item.label} className={item.status === "ok" ? "" : "warn"}>
              {MARK[item.status]} {item.label} : {item.detail}
            </li>
          ))}
        </ul>
      )}
      <button onClick={() => void load()}>Actualiser</button>
    </section>
  );
}
