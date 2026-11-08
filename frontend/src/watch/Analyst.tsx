import { clip } from "../chain/types";
import type { AnalystReading, SheetEvent } from "./types";

const NOVELTY: Record<string, string> = {
  new: "fait nouveau",
  reminder: "rappel d'un fait connu",
  unclear: "nouveauté incertaine",
  none: "",
};
const LEVEL: Record<string, string> = { low: "faible", medium: "moyenne", high: "élevée" };
const TRIGGER: Record<string, string> = {
  first: "première lecture",
  weekly: "lecture de la semaine",
  verdict: "après un changement de verdict",
  event: "après un événement sérieux",
};

function day(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.slice(0, 10) : d.toLocaleDateString("fr-FR", { day: "numeric", month: "long" });
}

const MAX_REFS = 3; // chips per line; the rest is counted
const CAPS = { changes: 2, counter: 2, watch: 3, aside: 3, weak: 2 }; // older, longer readings are cut on screen

/** Event chips: the event's letter when it is on the sheet. An event no longer shown (set aside, noise of a
 * measurement) gets no chip: only a count, never its title. */
function Refs(props: { ids: string[]; events: SheetEvent[]; onSelect: (id: string) => void }) {
  const shown = props.ids.map((id) => props.events.find((x) => x.id === id)).filter((e): e is SheetEvent => e !== undefined);
  const chips = shown.slice(0, MAX_REFS);
  const more = props.ids.length - chips.length;
  if (chips.length === 0 && more === 0) return null;
  return (
    <span className="analyst-refs">
      {chips.map((e) => (
        <button key={e.id} className={`letter ${e.status}`} title={clip(e.title, 140)} aria-label={`Événement ${e.letter} : ${clip(e.title, 140)}`} onClick={() => props.onSelect(e.id)}>
          {e.letter}
        </button>
      ))}
      {more > 0 && <span className="muted small">+{more}</span>}
    </span>
  );
}

/** The analyst's reading of the material: the headline, the situation and what really counts first; the rest
 * folded. Model texts, checked by the code, rendered as text and clipped. The verdict stays the code's. */
export function Analyst(props: { reading: AnalystReading | null; events: SheetEvent[]; onSelect: (id: string) => void }) {
  const r = props.reading;
  if (r === null) {
    return (
      <section className="panel">
        <p className="eyebrow">Lecture de l'analyste</p>
        <p className="muted">Pas encore de lecture : l'analyste relit la matière chaque semaine et quand un événement sérieux arrive.</p>
      </section>
    );
  }
  const n = r.note;
  const refs = (ids: string[]) => <Refs ids={ids} events={props.events} onSelect={props.onSelect} />;
  const aside = n.set_aside.slice(0, CAPS.aside).filter((s) => s.why.trim());
  return (
    <section className="panel analyst">
      <p className="eyebrow">Lecture de l'analyste</p>
      <h3>{clip(n.headline, 160)}</h3>
      <p className="muted small">
        {day(r.created_at)} · {TRIGGER[r.trigger] ?? r.trigger} · confiance {LEVEL[n.confidence.level] ?? n.confidence.level}
        {r.pending ? " · nouvelle lecture en cours" : ""}
      </p>
      {n.verdict_doubt.doubts && (
        <p className="warn">L'analyste doute du verdict, qui reste calculé par le code : {clip(n.verdict_doubt.reason, 400)}</p>
      )}
      {r.verdict_changed_since && <p className="muted small">Le verdict a changé depuis cette lecture ; la suivante en tiendra compte.</p>}
      <p className="analyst-situation">{clip(n.situation, 500)}</p>
      {n.main_signal.novelty !== "none" && (
        <div className="analyst-main">
          <p className="eyebrow">Ce qui compte vraiment</p>
          <p>
            {clip(n.main_signal.text, 400)} {refs(n.main_signal.event_ids)}
          </p>
          <p className="muted small">
            {NOVELTY[n.main_signal.novelty]}
            {n.main_signal.origin && n.main_signal.origin !== "unknown" ? ` · source d'origine : ${clip(n.main_signal.origin, 80)}` : ""}
          </p>
        </div>
      )}

      <details className="fold">
        <summary>Lire le détail</summary>
        <div className="situation-grid">
          <div>
            <p className="eyebrow">Ce qui a changé</p>
            <ul className="plain">
              {n.changes.slice(0, CAPS.changes).map((c, i) => (
                <li key={i}>
                  {clip(c.text, 240)} {refs(c.event_ids)}
                </li>
              ))}
            </ul>
            {n.counter_evidence.length > 0 && (
              <>
                <p className="eyebrow">Ce qui contredit l'alerte</p>
                <ul className="plain">
                  {n.counter_evidence.slice(0, CAPS.counter).map((c, i) => (
                    <li key={i}>
                      {clip(c.text, 240)} {refs(c.event_ids)}
                    </li>
                  ))}
                </ul>
              </>
            )}
            {aside.length > 0 && (
              <>
                <p className="eyebrow">Écarté</p>
                <ul className="plain muted">
                  {aside.map((s, i) => (
                    <li key={i}>{clip(s.why, 200)}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
          <div>
            {n.watch.length > 0 && (
              <>
                <p className="eyebrow">À surveiller</p>
                <ul className="plain">
                  {n.watch.slice(0, CAPS.watch).map((w, i) => (
                    <li key={i}>
                      <strong>{clip(w.what, 120)}</strong>
                      {w.date !== "unknown" && <span className="mono"> · {w.date}</span>} — {clip(w.why, 200)} {refs(w.event_ids)}
                    </li>
                  ))}
                </ul>
              </>
            )}
            <p className="eyebrow">Sur quoi repose la confiance</p>
            <p>{clip(n.confidence.basis, 300)}</p>
            {n.confidence.weak_points.length > 0 && (
              <ul className="plain muted small">
                {n.confidence.weak_points.slice(0, CAPS.weak).map((p, i) => (
                  <li key={i}>{clip(p, 160)}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </details>
      <p className="muted small">Texte rédigé par l'IA à partir des seules données publiques de la veille, vérifié par le code. Le verdict reste calculé par le code.</p>
    </section>
  );
}
