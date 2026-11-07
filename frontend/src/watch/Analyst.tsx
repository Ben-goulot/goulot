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

/** Event chips: the event's letter when it is on the sheet, else its title (a set-aside event). */
function Refs(props: { ids: string[]; events: SheetEvent[]; titles: Record<string, string>; onSelect: (id: string) => void }) {
  if (props.ids.length === 0) return null;
  return (
    <span className="analyst-refs">
      {props.ids.map((id) => {
        const e = props.events.find((x) => x.id === id);
        return e ? (
          <button key={id} className={`letter ${e.status}`} title={clip(e.title, 140)} onClick={() => props.onSelect(id)}>
            {e.letter}
          </button>
        ) : (
          <span key={id} className="muted small">
            « {clip(props.titles[id] ?? "événement écarté", 80)} »
          </span>
        );
      })}
    </span>
  );
}

/** The analyst's reading of the material: what is going on, what changed, what counts, what to watch, how far to
 * trust it. Model texts, checked by the code, rendered as text and clipped. The verdict stays the code's. */
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
  const refs = (ids: string[]) => <Refs ids={ids} events={props.events} titles={r.titles} onSelect={props.onSelect} />;
  return (
    <section className="panel analyst">
      <p className="eyebrow">Lecture de l'analyste</p>
      <h3>{clip(n.headline, 160)}</h3>
      <p className="muted small">
        {day(r.created_at)} · {TRIGGER[r.trigger] ?? r.trigger}
        {r.pending ? " · nouvelle lecture en cours" : ""}
      </p>
      {n.verdict_doubt.doubts && (
        <p className="warn">L'analyste doute du verdict, qui reste calculé par le code : {clip(n.verdict_doubt.reason, 400)}</p>
      )}
      {r.verdict_changed_since && <p className="muted small">Le verdict a changé depuis cette lecture ; la suivante en tiendra compte.</p>}
      <p>{clip(n.situation, 700)}</p>

      <details className="fold">
        <summary>Lire le détail : ce qui a changé, ce qui contredit l'alerte, à surveiller, confiance</summary>
      <div className="situation-grid">
        <div>
          <p className="eyebrow">Ce qui a changé</p>
          <ul className="plain">
            {n.changes.map((c, i) => (
              <li key={i}>
                {clip(c.text, 300)} {refs(c.event_ids)}
              </li>
            ))}
          </ul>
          <p className="eyebrow">Ce qui compte vraiment</p>
          <p>
            {clip(n.main_signal.text, 500)} {refs(n.main_signal.event_ids)}
          </p>
          {n.main_signal.novelty !== "none" && (
            <p className="muted small">
              {NOVELTY[n.main_signal.novelty]}
              {n.main_signal.origin && n.main_signal.origin !== "unknown" ? ` · source d'origine : ${clip(n.main_signal.origin, 80)}` : ""}
            </p>
          )}
          {n.set_aside.length > 0 && (
            <>
              <p className="eyebrow">Écarté ou relativisé</p>
              <ul className="plain">
                {n.set_aside.map((s, i) => (
                  <li key={i}>
                    {refs(s.event_id ? [s.event_id] : [])} {clip(s.why, 240)}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
        <div>
          {n.counter_evidence.length > 0 && (
            <>
              <p className="eyebrow">Ce qui contredit l'alerte</p>
              <ul className="plain">
                {n.counter_evidence.map((c, i) => (
                  <li key={i}>
                    {clip(c.text, 300)} {refs(c.event_ids)}
                  </li>
                ))}
              </ul>
            </>
          )}
          {n.watch.length > 0 && (
            <>
              <p className="eyebrow">À surveiller</p>
              <ul className="plain">
                {n.watch.map((w, i) => (
                  <li key={i}>
                    <strong>{clip(w.what, 140)}</strong>
                    {w.date !== "unknown" && <span className="mono"> · {w.date}</span>} — {clip(w.why, 240)} {refs(w.event_ids)}
                  </li>
                ))}
              </ul>
            </>
          )}
          <p className="eyebrow">Confiance : {LEVEL[n.confidence.level] ?? n.confidence.level}</p>
          <p>{clip(n.confidence.basis, 400)}</p>
          {n.confidence.weak_points.length > 0 && (
            <ul className="plain muted small">
              {n.confidence.weak_points.map((p, i) => (
                <li key={i}>{clip(p, 200)}</li>
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
