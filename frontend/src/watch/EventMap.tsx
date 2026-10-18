import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { useEffect, useRef } from "react";
import type { SheetEvent } from "./types";

// The worker is served by Goulot itself (worker-src 'self'); tiles and style come from OpenFreeMap, the only
// external host of the CSP (img-src, connect-src). No eval, no inline script.
maplibregl.setWorkerUrl(workerUrl);
const STYLE = "https://tiles.openfreemap.org/styles/dark";
const STATUS_COLOR = ["match", ["get", "status"], "ongoing", "#FF8A4C", "preparing", "#F5B841", "#B8C2CF"];

type Site = { name: string; lat: number; lon: number; type: string };

function features(points: { lat: number | null; lon: number | null; props: Record<string, string> }[]) {
  return {
    type: "FeatureCollection" as const,
    features: points
      .filter((p) => p.lat !== null && p.lon !== null)
      .map((p) => ({ type: "Feature" as const, geometry: { type: "Point" as const, coordinates: [p.lon!, p.lat!] }, properties: p.props })),
  };
}

export function EventMap(props: { events: SheetEvent[]; sites: Site[]; selected: string | null; onSelect: (id: string) => void }) {
  const box = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);

  useEffect(() => {
    if (!box.current) return;
    const map = new maplibregl.Map({ container: box.current, style: STYLE, center: [20, 30], zoom: 1.2, attributionControl: { compact: true } });
    mapRef.current = map;
    map.on("load", () => {
      map.addSource("sites", { type: "geojson", data: features(props.sites.map((s) => ({ lat: s.lat, lon: s.lon, props: { name: s.name } }))) });
      map.addSource("events", {
        type: "geojson",
        data: features(props.events.map((e) => ({ lat: e.lat, lon: e.lon, props: { id: e.id, letter: e.letter, status: e.status } }))),
      });
      map.addLayer({ id: "sites", type: "circle", source: "sites", paint: { "circle-radius": 4, "circle-color": "#8A95A5", "circle-opacity": 0.8 } });
      map.addLayer({
        id: "events",
        type: "circle",
        source: "events",
        paint: { "circle-radius": 9, "circle-color": STATUS_COLOR as unknown as string, "circle-stroke-color": "#0B0E13", "circle-stroke-width": 2 },
      });
      map.on("click", "events", (ev: maplibregl.MapLayerMouseEvent) => {
        const id = ev.features?.[0]?.properties?.id;
        if (typeof id === "string") props.onSelect(id);
      });
    });
    return () => {
      map.remove();
      mapRef.current = null;
    };
    // The map is rebuilt only when the data change, not on selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.events, props.sites]);

  useEffect(() => {
    const event = props.events.find((e) => e.id === props.selected);
    if (mapRef.current && event && event.lat !== null && event.lon !== null) {
      mapRef.current.flyTo({ center: [event.lon, event.lat], zoom: 4 });
    }
  }, [props.selected, props.events]);

  return <div className="map" ref={box} role="img" aria-label="Carte des événements et des sites de la chaîne" />;
}
