import { useState } from "react";
import { api, ApiError } from "../api";
import { clip } from "../chain/types";
import type { WorldHotspot } from "./types";

/** Every world hot spot, folded by default and read when opened. They never change a verdict. */
export function WorldHotspots() {
  const [spots, setSpots] = useState<WorldHotspot[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = (open: boolean) => {
    if (!open || spots) return;
    api.get<{ hotspots: WorldHotspot[] }>("/api/hotspots").then(
      (d) => setSpots(d.hotspots),
      (e) => setError(e instanceof ApiError ? e.message : "Erreur."),
    );
  };

  return (
    <details className="world-hotspots" onToggle={(e) => load(e.currentTarget.open)}>
      <summary>Points chauds mondiaux</summary>
      <p className="muted small">Alertes dans le monde, qu'elles touchent ou non tes chaînes. La carte d'une matière ne montre que celles qui touchent sa chaîne ; aucune ne change un verdict à elle seule.</p>
      {error && <p className="warn">{error}</p>}
      {!spots && !error && <p className="muted">Chargement…</p>}
      {spots?.length === 0 && <p className="muted">Aucun point chaud pour l'instant.</p>}
      {spots && spots.length > 0 && (
        <ul className="plain">
          {spots.map((s) => (
            <li key={s.id}>
              {s.source_url.startsWith("https://") ? (
                <a href={s.source_url} target="_blank" rel="noopener noreferrer">
                  {clip(s.label, 70)}
                </a>
              ) : (
                clip(s.label, 70)
              )}{" "}
              <span className="muted small">
                {clip(s.status, 40)} · {s.date}
              </span>
              <div className="small">
                {s.materials.length > 0 ? `Touche : ${clip(s.materials.join(", "), 120)}` : <span className="muted">Ne touche aucune de tes chaînes types</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
