import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { MODE } from "../chain/types";
import { WATCH_HELP, type City, type Link, type MyChain, type Site } from "./types";

const STYLE = "https://tiles.openfreemap.org/styles/positron";
const ROLES: [Site["role"], string][] = [
  ["producer", "Producteur"],
  ["processor", "Transformateur"],
  ["distributor", "Distributeur"],
  ["warehouse", "Entrepôt"],
  ["port", "Port"],
];
const WATCH: [Site["watch_level"], string][] = [
  ["city_material", "Ville et matière (par défaut)"],
  ["hazards", "Aléas seulement"],
  ["city_material_company", "Ville, matière et entreprise"],
];

function num(v: string): number | null {
  const t = v.trim().replace(",", ".");
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** Offline city lookup (GeoNames list embedded in the app): the query never leaves the Mac. */
function CitySearch(props: { value: string; country: string; onPick: (c: City) => void; onText: (v: string) => void }) {
  const [results, setResults] = useState<City[]>([]);
  useEffect(() => {
    const q = props.value.trim();
    if (q.length < 2) {
      setResults([]);
      return;
    }
    const id = window.setTimeout(() => {
      const country = /^[A-Z]{2}$/.test(props.country) ? `&country=${props.country}` : "";
      api.get<{ results: City[] }>(`/api/private/cities?q=${encodeURIComponent(q)}${country}`).then(
        (r) => setResults(r.results),
        () => setResults([]),
      );
    }, 250);
    return () => window.clearTimeout(id);
  }, [props.value, props.country]);
  return (
    <>
      <input value={props.value} maxLength={120} onChange={(e) => props.onText(e.target.value)} aria-label="Ville" />
      {results.length > 0 && (
        <ul className="plain suggestions">
          {results.map((c) => (
            <li key={`${c.name}-${c.lat}-${c.lon}`}>
              <button
                type="button"
                onClick={() => {
                  props.onPick(c);
                  setResults([]);
                }}
              >
                {c.name} <span className="muted">· {c.country_iso2}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/** A point placed on the map. The tiles shown tell OpenFreeMap which area is displayed (docs/SPEC.md, section 11). */
function PointPicker(props: { lat: number | null; lon: number | null; onPick: (lat: number, lon: number) => void }) {
  const box = useRef<HTMLDivElement | null>(null);
  const marker = useRef<maplibregl.Marker | null>(null);
  const pick = useRef(props.onPick);
  pick.current = props.onPick;
  useEffect(() => {
    if (!box.current) return;
    const center: [number, number] = props.lat !== null && props.lon !== null ? [props.lon, props.lat] : [10, 30];
    const map = new maplibregl.Map({ container: box.current, style: STYLE, center, zoom: props.lat !== null ? 6 : 1.2, attributionControl: { compact: true } });
    if (props.lat !== null && props.lon !== null) marker.current = new maplibregl.Marker({ color: "#6CB2FF" }).setLngLat(center).addTo(map);
    map.on("click", (ev) => {
      const lat = Math.round(ev.lngLat.lat * 1000) / 1000;
      const lon = Math.round(ev.lngLat.lng * 1000) / 1000;
      marker.current?.remove();
      marker.current = new maplibregl.Marker({ color: "#6CB2FF" }).setLngLat([lon, lat]).addTo(map);
      pick.current(lat, lon);
    });
    return () => map.remove();
    // Built once per opening of the form.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <div className="map small-map" ref={box} role="img" aria-label="Carte : clique pour placer le site" />;
}

export type SiteDraft = { site: Site; links: Link[] };

export function SiteForm(props: { chain: MyChain; draft: SiteDraft; onSave: (d: SiteDraft) => void; onCancel: () => void }) {
  const [site, setSite] = useState<Site>(props.draft.site);
  const [links, setLinks] = useState<Link[]>(props.draft.links);
  const [text, setText] = useState({ aliases: props.draft.site.aliases.join(", "), stock: props.draft.site.stock_days?.toString() ?? "" });
  const [showMap, setShowMap] = useState(false);
  const others = props.chain.sites.filter((s) => s.id !== site.id);

  const setLink = (i: number, change: Partial<Link>) => setLinks(links.map((l, j) => (j === i ? { ...l, ...change } : l)));
  const save = () =>
    props.onSave({
      site: {
        ...site,
        aliases: text.aliases.split(",").map((a) => a.trim()).filter(Boolean).slice(0, 10),
        stock_days: num(text.stock),
      },
      links,
    });

  return (
    <div className="form">
      <label>
        Site ou entreprise
        <input value={site.name} maxLength={120} onChange={(e) => setSite({ ...site, name: e.target.value })} />
      </label>
      <label>
        Rôle
        <select value={site.role} onChange={(e) => setSite({ ...site, role: e.target.value as Site["role"] })}>
          {ROLES.map(([k, label]) => (
            <option key={k} value={k}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <p className="eyebrow">Ce qu'il fournit</p>
      {links.map((l, i) => (
        <div key={i} className="link-row">
          <select value={l.target} aria-label="Fournit" onChange={(e) => setLink(i, { target: e.target.value })}>
            <option value="me">toi</option>
            {others.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <label>
            Part (%)
            <input inputMode="decimal" value={l.share_pct} onChange={(e) => setLink(i, { share_pct: num(e.target.value) ?? 0 })} />
          </label>
          <label>
            Délai (jours)
            <input inputMode="decimal" value={l.lead_days ?? ""} onChange={(e) => setLink(i, { lead_days: num(e.target.value) })} />
          </label>
          <select value={l.transport_mode} aria-label="Transport" onChange={(e) => setLink(i, { transport_mode: e.target.value })}>
            {Object.entries(MODE).map(([k, label]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
          <input value={l.incoterm} maxLength={12} placeholder="Incoterm" aria-label="Incoterm" onChange={(e) => setLink(i, { incoterm: e.target.value })} />
          <button type="button" onClick={() => setLinks(links.filter((_, j) => j !== i))}>
            Retirer
          </button>
        </div>
      ))}
      <button type="button" onClick={() => setLinks([...links, { source: site.id, target: "me", share_pct: 100, transport_mode: "unknown", incoterm: "", lead_days: null }])}>
        Ajouter un client de ce site
      </button>
      <p className="muted small">Part : part des achats du client qui vient de ce site (pour toi : part de ton volume).</p>

      <p className="eyebrow">Lieu (hors ligne)</p>
      <label>
        Ville
        <CitySearch
          value={site.city}
          country={site.country_iso2}
          onText={(city) => setSite({ ...site, city })}
          onPick={(c) => setSite({ ...site, city: c.name, country_iso2: c.country_iso2, lat: c.lat, lon: c.lon })}
        />
      </label>
      <label>
        Pays (code ISO à 2 lettres)
        <input value={site.country_iso2} maxLength={2} onChange={(e) => setSite({ ...site, country_iso2: e.target.value.toUpperCase() })} />
      </label>
      <p className="muted small">
        Point : {site.lat !== null && site.lon !== null ? `${site.lat}, ${site.lon}` : "aucun"}{" "}
        <button type="button" onClick={() => setShowMap(!showMap)}>
          {showMap ? "Fermer la carte" : "Placer sur la carte"}
        </button>
      </p>
      {showMap && <PointPicker lat={site.lat} lon={site.lon} onPick={(lat, lon) => setSite({ ...site, lat, lon })} />}

      <label>
        Surveillance en ligne
        <select value={site.watch_level} onChange={(e) => setSite({ ...site, watch_level: e.target.value as Site["watch_level"] })}>
          {WATCH.map(([k, label]) => (
            <option key={k} value={k}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <p className="muted small">{WATCH_HELP[site.watch_level]}</p>
      <label>
        Entreprise (envoyée seulement au niveau « ville, matière et entreprise »)
        <input value={site.company} maxLength={120} onChange={(e) => setSite({ ...site, company: e.target.value })} />
      </label>
      <label>
        Autres noms, dont la langue locale (séparés par des virgules)
        <input value={text.aliases} maxLength={600} onChange={(e) => setText({ ...text, aliases: e.target.value })} />
      </label>
      <label>
        Stock chez lui, en jours (s'il le communique)
        <input inputMode="decimal" value={text.stock} onChange={(e) => setText({ ...text, stock: e.target.value })} />
      </label>

      <p className="eyebrow">Plan B</p>
      {site.alternatives.map((a, i) => (
        <div key={i} className="link-row">
          <input value={a.name} maxLength={120} aria-label="Fournisseur de remplacement" onChange={(e) => setSite({ ...site, alternatives: site.alternatives.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)) })} />
          <select value={a.status} aria-label="Statut" onChange={(e) => setSite({ ...site, alternatives: site.alternatives.map((x, j) => (j === i ? { ...x, status: e.target.value as typeof a.status } : x)) })}>
            <option value="qualified">qualifié</option>
            <option value="qualifying">en cours de qualification</option>
            <option value="not_qualified">non qualifié</option>
          </select>
          <label>
            Bascule (sem.)
            <input inputMode="decimal" value={a.switch_weeks ?? ""} onChange={(e) => setSite({ ...site, alternatives: site.alternatives.map((x, j) => (j === i ? { ...x, switch_weeks: num(e.target.value) } : x)) })} />
          </label>
          <button type="button" onClick={() => setSite({ ...site, alternatives: site.alternatives.filter((_, j) => j !== i) })}>
            Retirer
          </button>
        </div>
      ))}
      <button type="button" onClick={() => setSite({ ...site, alternatives: [...site.alternatives, { name: "", status: "qualified", switch_weeks: null }] })}>
        Ajouter une alternative
      </button>
      <label>
        Notes
        <input value={site.notes} maxLength={1000} onChange={(e) => setSite({ ...site, notes: e.target.value })} />
      </label>
      <div className="row">
        <button className="primary" disabled={!site.name.trim()} onClick={save}>
          Enregistrer dans le coffre
        </button>
        <button onClick={props.onCancel}>Annuler</button>
      </div>
      <p className="muted small">Rien de ce que tu saisis ne part vers l'IA : tout reste chiffré sur ce Mac.</p>
    </div>
  );
}
