import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { useEffect, useMemo, useRef, useState } from "react";
import { clip, CONFIDENCE, COORD_SOURCE, NODE_TYPE, safeHref, type CoordSource } from "../chain/types";
import {
  COUNTRY,
  counts,
  countryOpacity,
  EVENT_LOOK,
  FLOW,
  GREY,
  KIND,
  LEGEND,
  legendOf,
  loadHidden,
  MODE_DASH,
  OUTLINE,
  ROLE_KIND,
  saveHidden,
  VERMILION,
  type LegendKey,
  type Shape,
} from "./style";

// The worker is served by Goulot itself (worker-src 'self'); tiles and style come from OpenFreeMap, the only
// external host of the CSP (img-src, connect-src). No eval, no inline script, no HTML built from data: every text
// from the chain or an event is set with textContent.
maplibregl.setWorkerUrl(workerUrl);
export const TILE_STYLE = "https://tiles.openfreemap.org/styles/positron";

export type MapNode = {
  node_key: string;
  node_type: string;
  name: string;
  product: string;
  country_iso2: string;
  global_share_pct: number | null;
  confidence: "high" | "medium" | "low";
  coord_source: CoordSource;
  added_by: "" | "goulot";
  lat: number;
  lon: number;
  exposed_by: string[];
};
export type MapFlow = { from: string; to: string; mode: string; via: string[]; approximate: boolean; coords: number[][]; exposed_by: string[] };
export type MapEvent = {
  id: string;
  letter: string;
  title: string;
  place: string;
  status: "ongoing" | "preparing" | "early" | "ended";
  status_label: string;
  lat: number | null;
  lon: number | null;
  nodes: string[];
  node_keys: string[];
};
export type MapZone = {
  id: string;
  kind: "event" | "hotspot";
  event_id: string | null;
  label: string; // external text (event or source): shown as text only
  status: string;
  date: string;
  lat: number;
  lon: number;
  rings: number[][][];
  sources: { title: string; url: string; publisher: string }[];
};
export type MapData = {
  nodes: MapNode[];
  flows: MapFlow[];
  countries: { type: "FeatureCollection"; features: { type: "Feature"; properties: { iso2: string; share: number }; geometry: unknown }[] };
  bounds: [number, number, number, number] | null;
  zones: MapZone[];
  events: MapEvent[];
};
/** A site of Ma chaîne: passed only while the vault is open, never used to frame the view. */
export type MineSite = { id: string; name: string; role: string; city: string; country_iso2: string; lat: number; lon: number };

type Picked = { kind: "node"; key: string } | { kind: "event"; id: string } | { kind: "mine"; id: string } | { kind: "zone"; id: string };

const pickKey = (p: Picked) => `${p.kind}:${p.kind === "node" ? p.key : p.id}`;
const SVG = "http://www.w3.org/2000/svg";
const countryName = (() => {
  try {
    const names = new Intl.DisplayNames(["fr"], { type: "region" });
    return (iso: string) => (iso === "unknown" ? "pays inconnu" : (names.of(iso) ?? iso));
  } catch {
    return (iso: string) => iso;
  }
})();

function svgShape(shape: Shape, fill: string, stroke: string, width: number, dashed: boolean): SVGElement {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "24");
  svg.setAttribute("height", "24");
  svg.setAttribute("aria-hidden", "true");
  const el = document.createElementNS(SVG, shape === "square" ? "rect" : shape === "circle" || shape === "ring" ? "circle" : "polygon");
  if (shape === "square") {
    el.setAttribute("x", "4");
    el.setAttribute("y", "4");
    el.setAttribute("width", "16");
    el.setAttribute("height", "16");
  } else if (shape === "circle" || shape === "ring") {
    el.setAttribute("cx", "12");
    el.setAttribute("cy", "12");
    el.setAttribute("r", "8");
  } else {
    const points = { triangle: "12,3 21,20 3,20", diamond: "12,2 22,12 12,22 2,12", hexagon: "12,2 21,7 21,17 12,22 3,17 3,7" }[shape];
    el.setAttribute("points", points);
  }
  el.setAttribute("fill", fill);
  el.setAttribute("stroke", stroke);
  el.setAttribute("stroke-width", String(width));
  if (dashed) el.setAttribute("stroke-dasharray", "3 2");
  svg.appendChild(el);
  return svg;
}

function nodeMarker(nodeType: string, approximate: boolean, mine: boolean): HTMLButtonElement {
  const look = KIND[nodeType] ?? { shape: "circle" as Shape, color: GREY };
  const button = document.createElement("button");
  button.type = "button";
  button.className = mine ? "map-marker mine" : "map-marker";
  let svg: SVGElement;
  if (look.shape === "ring") svg = svgShape("ring", "#FFFFFF", look.color, 3.5, approximate);
  else if (approximate) svg = svgShape(look.shape, "#FFFFFF", look.color, 2.5, true);
  else svg = svgShape(look.shape, look.color, mine ? "#000000" : OUTLINE, mine ? 3.5 : 1.5, false);
  button.appendChild(svg);
  if (mine) {
    const badge = document.createElement("span");
    badge.className = "map-badge";
    badge.textContent = "moi";
    button.appendChild(badge);
  }
  return button;
}

function eventMarker(e: MapEvent): HTMLButtonElement {
  const look = EVENT_LOOK[e.status] ?? EVENT_LOOK.early;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "map-marker event";
  const svg = svgShape("circle", look.filled ? look.color : "#FFFFFF", look.filled ? OUTLINE : look.color, look.filled ? 1.5 : 2.5, look.dashed);
  const text = document.createElementNS(SVG, "text");
  text.setAttribute("x", "12");
  text.setAttribute("y", "16");
  text.setAttribute("text-anchor", "middle");
  text.setAttribute("font-size", "12");
  text.setAttribute("font-weight", "700");
  text.setAttribute("fill", look.filled ? "#FFFFFF" : look.color === GREY ? "#3D3D3D" : VERMILION);
  text.textContent = e.status === "ongoing" ? "!" : e.letter;
  svg.appendChild(text);
  button.appendChild(svg);
  return button;
}

/** Diagonal hatching of the alert zones, so that a zone is told by its texture as well as its color. */
function hatchImage(): ImageData | null {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 12;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.strokeStyle = VERMILION;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (const o of [-12, 0, 12]) {
      ctx.moveTo(o, 12);
      ctx.lineTo(o + 12, 0);
    }
    ctx.stroke();
    return ctx.getImageData(0, 0, 12, 12);
  } catch {
    return null;
  }
}

function zoneLabel(z: MapZone): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = z.kind === "event" ? "map-zone-label" : "map-zone-label hotspot";
  const head = document.createElement("span");
  const icon = document.createElement("span");
  icon.textContent = "⚠";
  const title = document.createElement("span");
  title.className = "map-zone-text";
  title.textContent = ` ${z.label}`;
  head.append(icon, title);
  const sub = document.createElement("span");
  sub.className = "map-zone-sub";
  sub.textContent = `${z.status} · ${z.date}`;
  button.append(head, sub);
  return button;
}

function lines(rows: string[]): HTMLElement {
  const box = document.createElement("div");
  rows.forEach((row, i) => {
    const p = document.createElement(i === 0 ? "strong" : "div");
    p.textContent = row;
    box.appendChild(p);
  });
  return box;
}

function nodeRows(n: MapNode, events: MapEvent[]): string[] {
  const current = events.filter((e) => e.node_keys.includes(n.node_key) && e.status !== "ended");
  return [
    clip(n.name, 80),
    `${NODE_TYPE[n.node_type] ?? n.node_type}${n.product ? ` · ${clip(n.product, 60)}` : ""}`,
    countryName(n.country_iso2),
    n.global_share_pct !== null ? `Part mondiale : ${n.global_share_pct} %` : "Part mondiale inconnue",
    `Confiance : ${CONFIDENCE[n.confidence]}`,
    `Position : ${COORD_SOURCE[n.coord_source]}${n.added_by === "goulot" ? " · route probable, ajoutée par Goulot" : ""}`,
    current.length ? `Événements : ${current.map((e) => `${e.letter} ${clip(e.title, 50)}`).join(" ; ")}` : "Aucun événement en cours",
  ];
}

function eventRows(e: MapEvent): string[] {
  return [`${e.letter} · ${clip(e.title, 80)}`, e.status_label, e.place ? clip(e.place, 80) : "", e.nodes.length ? `Maillons : ${e.nodes.map((n) => clip(n, 40)).join(", ")}` : ""].filter(Boolean);
}

export function ChainMap(props: {
  data: MapData;
  mine?: MineSite[] | null;
  selectedEvent?: string | null;
  onEvent?: (id: string) => void;
  onShowInGraph?: (nodeKey: string) => void;
}) {
  const box = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const [hidden, setHidden] = useState<Set<LegendKey>>(() => loadHidden());
  const [picked, setPicked] = useState<Picked | null>(null);
  const { data } = props;

  const flows = useMemo(
    () => ({
      type: "FeatureCollection" as const,
      features: data.flows.map((f) => ({
        type: "Feature" as const,
        geometry: { type: "LineString" as const, coordinates: f.coords },
        properties: { dash: MODE_DASH[f.mode] ?? "dotted", exposed: f.exposed_by.length > 0 },
      })),
    }),
    [data.flows],
  );
  const legendCounts = useMemo(
    () =>
      counts(data.nodes, {
        country: data.countries.features.length,
        flow: data.flows.length,
        event: data.events.filter((e) => e.lat !== null).length,
        zone: data.zones.filter((z) => z.kind === "event").length,
        hotspot: data.zones.filter((z) => z.kind === "hotspot").length,
      }),
    [data],
  );

  // The map itself, built once per data set. The default view frames the typical chain only.
  useEffect(() => {
    if (!box.current) return;
    const map = new maplibregl.Map({ container: box.current, style: TILE_STYLE, center: [20, 30], zoom: 1.2, attributionControl: { compact: true } });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    // Zoomed out, zone labels shrink to « ⚠ » (full text on hover, focus and in the label read aloud).
    const compact = () => box.current?.classList.toggle("zoomed-out", map.getZoom() < 3.5);
    map.on("zoom", compact);
    map.on("load", () => {
      const max = Math.max(0, ...data.countries.features.map((f) => f.properties.share));
      map.addSource("countries", {
        type: "geojson",
        data: {
          ...data.countries,
          features: data.countries.features.map((f) => ({ ...f, properties: { ...f.properties, opacity: countryOpacity(f.properties.share, max) } })),
        } as maplibregl.GeoJSONSourceSpecification["data"],
      });
      map.addLayer({ id: "countries", type: "fill", source: "countries", paint: { "fill-color": COUNTRY, "fill-opacity": ["get", "opacity"] } });
      map.addSource("zones", {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: data.zones.map((z) => ({ type: "Feature", properties: { id: z.id, kind: z.kind }, geometry: { type: "MultiPolygon", coordinates: z.rings.map((r) => [r]) } })),
        } as maplibregl.GeoJSONSourceSpecification["data"],
      });
      const hatch = hatchImage();
      if (hatch) map.addImage("hatch", hatch);
      map.addLayer({ id: "zones", type: "fill", source: "zones", paint: { "fill-color": VERMILION, "fill-opacity": 0.18 } });
      if (hatch) map.addLayer({ id: "zones-hatch", type: "fill", source: "zones", paint: { "fill-pattern": "hatch", "fill-opacity": 0.7 } });
      map.addLayer({ id: "zones-line", type: "line", source: "zones", paint: { "line-color": VERMILION, "line-width": 2, "line-dasharray": [3, 2] } });
      map.addSource("flows", { type: "geojson", data: flows as maplibregl.GeoJSONSourceSpecification["data"] });
      const base = { "line-color": FLOW, "line-width": 2 };
      map.addLayer({ id: "flows-sea", type: "line", source: "flows", filter: ["==", ["get", "dash"], "sea"], paint: { ...base, "line-dasharray": [4, 3] } });
      map.addLayer({ id: "flows-solid", type: "line", source: "flows", filter: ["==", ["get", "dash"], "solid"], paint: base });
      map.addLayer({
        id: "flows-dotted",
        type: "line",
        source: "flows",
        filter: ["==", ["get", "dash"], "dotted"],
        layout: { "line-cap": "round" },
        paint: { ...base, "line-dasharray": [0.1, 2.2] },
      });
      map.addLayer({ id: "flows-exposed", type: "line", source: "flows", filter: ["==", ["get", "exposed"], true], paint: { "line-color": VERMILION, "line-width": 4.5 } });
      if (data.bounds) {
        const [w, s, e, n] = data.bounds;
        if (e - w < 0.5 && n - s < 0.5) map.jumpTo({ center: [(w + e) / 2, (s + n) / 2], zoom: 4 });
        else map.fitBounds([[w, s], [e, n]], { padding: 48, maxZoom: 5, animate: false });
      }
      compact();
      setReady(true);
    });
    return () => {
      setReady(false);
      map.remove();
      mapRef.current = null;
    };
  }, [data, flows]);

  // Legend filter: layers and markers follow it.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const show = (layer: string, key: LegendKey) => map.setLayoutProperty(layer, "visibility", hidden.has(key) ? "none" : "visible");
    show("countries", "country");
    ["flows-sea", "flows-solid", "flows-dotted", "flows-exposed"].forEach((l) => show(l, "flow"));
    const kinds = [...(hidden.has("zone") ? [] : ["event"]), ...(hidden.has("hotspot") ? [] : ["hotspot"])];
    for (const layer of ["zones", "zones-hatch", "zones-line"]) {
      if (map.getLayer(layer)) map.setFilter(layer, ["in", ["get", "kind"], ["literal", kinds]]);
    }
    saveHidden(hidden);
  }, [hidden, ready]);

  // Markers: buttons, reachable with the keyboard; hover or focus shows a summary, a click opens the detail.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const markers: maplibregl.Marker[] = [];
    const tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 16, className: "map-tip", maxWidth: "320px" });
    const wire = (el: HTMLButtonElement, lon: number, lat: number, label: string, rows: string[], pick: Picked) => {
      el.setAttribute("aria-label", label);
      const open = () => tip.setLngLat([lon, lat]).setDOMContent(lines(rows)).addTo(map);
      el.addEventListener("mouseenter", open);
      el.addEventListener("focus", open);
      el.addEventListener("mouseleave", () => tip.remove());
      el.addEventListener("blur", () => tip.remove());
      el.addEventListener("click", () => {
        setPicked(pick);
        if (pick.kind === "event") props.onEvent?.(pick.id);
      });
      if (picked && pickKey(picked) === pickKey(pick)) el.classList.add("selected");
    };
    const add = (el: HTMLButtonElement, lon: number, lat: number, label: string, rows: string[], pick: Picked) => {
      wire(el, lon, lat, label, rows, pick);
      markers.push(new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map));
    };
    for (const n of data.nodes) {
      const key = legendOf(n.node_type);
      if (key && hidden.has(key)) continue;
      const approximate = n.coord_source === "country" || n.coord_source === "none";
      const inZones = data.zones.filter((z) => n.exposed_by.includes(z.id));
      const rows = [...nodeRows(n, data.events), ...(inZones.length ? [`Dans une zone d'alerte : ${inZones.map((z) => z.label).join(" ; ")}`] : [])];
      const el = nodeMarker(n.node_type, approximate, false);
      if (inZones.length) el.classList.add("exposed");
      add(el, n.lon, n.lat, `${rows[0]}, ${rows[1]}${inZones.length ? ", dans une zone d'alerte" : ""}`, rows, { kind: "node", key: n.node_key });
    }
    for (const s of props.mine ?? []) {
      const rows = [clip(s.name, 80), `Site de Ma chaîne${s.city ? ` · ${clip(s.city, 60)}` : ""}`, countryName(s.country_iso2)];
      add(nodeMarker(ROLE_KIND[s.role] ?? "distributor", false, true), s.lon, s.lat, `${rows[0]}, site de Ma chaîne`, rows, { kind: "mine", id: s.id });
    }
    // Zone labels: one column per spot, beside its centre, so that labels never pile up and the zone stays visible.
    const spots = new Map<string, MapZone[]>();
    for (const z of data.zones) {
      if (hidden.has(z.kind === "event" ? "zone" : "hotspot")) continue;
      const spot = `${z.lat.toFixed(1)},${z.lon.toFixed(1)}`;
      spots.set(spot, [...(spots.get(spot) ?? []), z]);
    }
    for (const group of spots.values()) {
      const column = document.createElement("div");
      column.className = "map-zone-stack";
      for (const z of group) {
        const kind = z.kind === "event" ? "Zone d'alerte" : "Point chaud mondial";
        const el = zoneLabel(z);
        wire(el, z.lon, z.lat, `${kind} : ${z.label}, ${z.status}, ${z.date}`, [`⚠ ${z.label}`, kind, `${z.status} · depuis le ${z.date}`], { kind: "zone", id: z.id });
        column.appendChild(el);
      }
      markers.push(new maplibregl.Marker({ element: column, anchor: "left", offset: [18, 0] }).setLngLat([group[0].lon, group[0].lat]).addTo(map));
    }
    if (!hidden.has("event")) {
      for (const e of data.events) {
        if (e.lat === null || e.lon === null) continue;
        add(eventMarker(e), e.lon, e.lat, `Événement ${e.letter}, ${e.status_label} : ${clip(e.title, 80)}`, eventRows(e), { kind: "event", id: e.id });
      }
    }
    return () => {
      tip.remove();
      markers.forEach((m) => m.remove());
    };
    // props.onEvent is a callback from the parent; markers do not depend on its identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, hidden, ready, props.mine, picked]);

  // An event chosen in the list is shown; a site of Ma chaîne never moves the view (the tiles would tell where).
  useEffect(() => {
    const event = data.events.find((e) => e.id === props.selectedEvent);
    if (mapRef.current && ready && event && event.lat !== null && event.lon !== null) {
      mapRef.current.flyTo({ center: [event.lon, event.lat], zoom: Math.max(mapRef.current.getZoom(), 4) });
      setPicked({ kind: "event", id: event.id });
    }
  }, [props.selectedEvent, data.events, ready]);

  const toggle = (key: LegendKey) =>
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const detail = picked ? <Detail picked={picked} data={data} mine={props.mine ?? []} onClose={() => setPicked(null)} onShowInGraph={props.onShowInGraph} /> : null;

  return (
    <div className="chain-map">
      <fieldset className="map-legend">
        <legend>Légende : décocher pour masquer</legend>
        {LEGEND.map((l) => (
          <label key={l.key} className={legendCounts[l.key] === 0 ? "muted" : undefined}>
            <input type="checkbox" checked={!hidden.has(l.key)} onChange={() => toggle(l.key)} />
            <LegendSwatch k={l.key} /> {l.label} <span className="mono">({legendCounts[l.key]})</span>
          </label>
        ))}
        {props.mine && props.mine.length > 0 && (
          <span className="map-legend-note">
            <LegendSwatch k="me" /> Site de Ma chaîne (coffre ouvert)
          </span>
        )}
        <span className="map-legend-note">Forme creuse en pointillés : position approximative · trait vermillon : flux exposé</span>
      </fieldset>
      <div className="map" ref={box} role="region" aria-label="Carte de la chaîne : sites, flux, pays producteurs et événements" />
      {detail}
    </div>
  );
}

function LegendSwatch(props: { k: LegendKey | "me" }) {
  const ref = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.replaceChildren();
    const nodeType = Object.entries(KIND).find(([, v]) => v.legend === props.k)?.[0];
    if (props.k === "me") el.appendChild(svgShape("square", KIND.plant.color, "#000000", 3.5, false));
    else if (nodeType) el.appendChild(svgShape(KIND[nodeType].shape, KIND[nodeType].shape === "ring" ? "#FFFFFF" : KIND[nodeType].color, KIND[nodeType].shape === "ring" ? GREY : OUTLINE, KIND[nodeType].shape === "ring" ? 3.5 : 1.5, false));
    else if (props.k === "event") el.appendChild(svgShape("circle", VERMILION, OUTLINE, 1.5, false));
    else if (props.k === "zone" || props.k === "hotspot") el.appendChild(svgShape("square", "#F7D4BC", VERMILION, 2, props.k === "hotspot"));
    else if (props.k === "country") el.appendChild(svgShape("square", "#7FB2D6", "#7FB2D6", 1, false));
    else {
      const svg = svgShape("square", "none", "none", 0, false);
      const line = document.createElementNS(SVG, "line");
      for (const [k, v] of Object.entries({ x1: "2", y1: "12", x2: "22", y2: "12", stroke: FLOW, "stroke-width": "2.5", "stroke-dasharray": "4 3" })) line.setAttribute(k, v);
      svg.appendChild(line);
      el.appendChild(svg);
    }
  }, [props.k]);
  return <span className="map-swatch" ref={ref} aria-hidden="true" />;
}

function Detail(props: { picked: Picked; data: MapData; mine: MineSite[]; onClose: () => void; onShowInGraph?: (key: string) => void }) {
  const { picked, data } = props;
  let rows: string[] = [];
  let nodeKey: string | null = null;
  if (picked.kind === "node") {
    const n = data.nodes.find((x) => x.node_key === picked.key);
    if (n) {
      rows = nodeRows(n, data.events);
      nodeKey = n.node_key;
    }
  } else if (picked.kind === "event") {
    const e = data.events.find((x) => x.id === picked.id);
    if (e) rows = eventRows(e);
  } else if (picked.kind === "zone") {
    const z = data.zones.find((x) => x.id === picked.id);
    if (z) return <ZoneDetail zone={z} event={data.events.find((e) => e.id === z.event_id) ?? null} onClose={props.onClose} />;
  } else {
    const s = props.mine.find((x) => x.id === picked.id);
    if (s) rows = [clip(s.name, 80), "Site de Ma chaîne", s.city ? clip(s.city, 60) : "", countryName(s.country_iso2)].filter(Boolean);
  }
  if (rows.length === 0) return null;
  return (
    <div className={picked.kind === "mine" ? "map-detail pv" : "map-detail"} role="dialog" aria-label="Détail" onKeyDown={(e) => e.key === "Escape" && props.onClose()}>
      <h3>{rows[0]}</h3>
      {rows.slice(1).map((r) => (
        <p key={r}>{r}</p>
      ))}
      <div className="row">
        {nodeKey && props.onShowInGraph && (
          <button className="primary" onClick={() => props.onShowInGraph?.(nodeKey!)}>
            Voir dans le graphe
          </button>
        )}
        <button onClick={props.onClose} autoFocus>
          Fermer
        </button>
      </div>
    </div>
  );
}

function ZoneDetail(props: { zone: MapZone; event: MapEvent | null; onClose: () => void }) {
  const { zone, event } = props;
  return (
    <div className="map-detail" role="dialog" aria-label="Détail de la zone d'alerte" onKeyDown={(e) => e.key === "Escape" && props.onClose()}>
      <h3>⚠ {zone.label}</h3>
      <p>
        {zone.kind === "event" ? "Zone d'alerte d'un événement de la chaîne" : "Point chaud mondial : il ne change pas le verdict par lui-même"} · {zone.status} ·
        depuis le {zone.date}
      </p>
      {event && (
        <p>
          Événement {event.letter} : {clip(event.title, 160)}
          {event.nodes.length > 0 && ` · maillons : ${event.nodes.map((n) => clip(n, 40)).join(", ")}`}
        </p>
      )}
      <p className="eyebrow">Sources</p>
      <ul className="plain">
        {zone.sources.map((s, i) => {
          const href = safeHref(s.url);
          return (
            <li key={`${s.url}-${i}`}>
              {href ? (
                <a href={href} target="_blank" rel="noopener noreferrer" className="source">
                  {clip(s.title || s.publisher, 120)}
                </a>
              ) : (
                clip(s.title || s.publisher, 120)
              )}
              {s.publisher && <span className="muted"> · {clip(s.publisher, 60)}</span>}
            </li>
          );
        })}
      </ul>
      <div className="row">
        <button onClick={props.onClose} autoFocus>
          Fermer
        </button>
      </div>
    </div>
  );
}
