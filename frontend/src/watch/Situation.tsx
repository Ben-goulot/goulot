import { clip } from "../chain/types";
import type { SituationEvent } from "./types";

function List(props: { title: string; items: string[] }) {
  if (props.items.length === 0) return null;
  return (
    <>
      <p className="eyebrow">{props.title}</p>
      <ul className="plain">
        {props.items.slice(0, 5).map((t, i) => (
          <li key={i}>{clip(t, 240)}</li>
        ))}
      </ul>
    </>
  );
}

function facts(e: SituationEvent): string {
  const parts = [`${e.status_label}`, `${e.lineages} source${e.lineages > 1 ? "s" : ""} indépendante${e.lineages > 1 ? "s" : ""}`];
  if (e.severity !== null) parts.push(`sévérité ${e.severity} sur 4`);
  if (e.duration_high !== null) parts.push(e.duration_low !== null && e.duration_low !== e.duration_high ? `durée estimée ${e.duration_low} à ${e.duration_high} jours` : `durée estimée ${e.duration_high} jours`);
  if (e.confidence !== null) parts.push(`confiance de l'analyse ${(e.confidence * 100).toFixed(0)} %`);
  return parts.join(" · ");
}

/** What justifies the verdict: a summary of the situation, event by event, then the sources read. Every text comes
 * from the sources or the model: rendered as text, clipped. */
export function Situation(props: { events: SituationEvent[]; onSelect: (id: string) => void }) {
  return (
    <>
      {props.events.map((e) => (
        <article key={e.id} className="situation">
          <p className="eyebrow">La situation</p>
          <button className="situation-event" onClick={() => props.onSelect(e.id)}>
            <span className="letter ongoing">{e.letter}</span> {clip(e.headline || e.title, 200)}
          </button>
          <p>{clip(e.summary, 900) || "Pas encore de résumé pour cet événement."}</p>
          {!e.analysed && <p className="warn">Pas encore d'analyse par l'IA : résumé tiré du tri de l'article.</p>}
          <p className="muted small">
            {clip(e.place, 60)}
            {e.place ? " · " : ""}
            {facts(e)}
            {e.nodes.length > 0 ? ` · maillon : ${clip(e.nodes.join(", "), 80)}` : ""}
          </p>
          <div className="situation-grid">
            <div>
              <List title="Comment ça se transmet" items={e.transmission} />
              <List title="Ce qui amortit" items={e.buffers} />
              <List title="Ce qui reste incertain" items={e.uncertainties} />
            </div>
            <div>
              {e.triggers.length > 0 && (
                <>
                  <p className="eyebrow">Ce qui changerait l'avis</p>
                  <ul className="plain">
                    {e.triggers.slice(0, 3).map((t, i) => (
                      <li key={i}>
                        {clip(t.signal, 200)} <span className="muted">· {clip(t.implication, 200)}</span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {e.sources.length > 0 && (
                <>
                  <p className="eyebrow">Sources</p>
              <ul className="plain">
                {e.sources.map((s, i) => (
                  <li key={i}>
                    {s.url.startsWith("https://") ? (
                      <a href={s.url} target="_blank" rel="noopener noreferrer">
                        {clip(s.title, 120)}
                      </a>
                    ) : (
                      clip(s.title, 120)
                    )}{" "}
                    <span className="muted small">
                      {clip(s.publisher, 40)}
                      {s.published_at ? ` · ${s.published_at.slice(0, 10)}` : ""} · fiabilité {s.reliability.toFixed(1).replace(".", ",")}
                    </span>
                  </li>
                ))}
              </ul>
                </>
              )}
            </div>
          </div>
        </article>
      ))}
    </>
  );
}
