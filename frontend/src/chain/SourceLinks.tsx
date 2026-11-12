import { clip, safeHref, type Source } from "./types";

/** Sources of a node or a proposal: only https links from the code's source table are rendered as links. */
export function SourceLinks(props: { refs: string[]; sources: Map<string, Source> }) {
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
