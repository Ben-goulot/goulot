// Map conventions (docs/SPEC.md, section 8, « Carte »). Color is never the only code: each type has its own shape,
// and every marker a dark 1.5 px outline. Okabe-Ito palette, readable with red-green color blindness; vermilion is
// kept for alerts only.

export const VERMILION = "#D55E00";
export const GREY = "#6B6B6B";
export const OUTLINE = "#1A1A1A";
export const FLOW = "#3D3D3D";
export const COUNTRY = "#0072B2";

export type Shape = "square" | "circle" | "triangle" | "diamond" | "hexagon" | "ring";
export type LegendKey = "plant" | "port" | "mine" | "recycler" | "passage" | "trader" | "country" | "flow" | "event" | "zone" | "hotspot";

export const KIND: Record<string, { legend: LegendKey; shape: Shape; color: string }> = {
  plant: { legend: "plant", shape: "square", color: "#0072B2" },
  port: { legend: "port", shape: "circle", color: "#56B4E9" },
  mine: { legend: "mine", shape: "triangle", color: "#000000" },
  recycler: { legend: "recycler", shape: "diamond", color: "#009E73" },
  chokepoint: { legend: "passage", shape: "hexagon", color: "#CC79A7" },
  waterway: { legend: "passage", shape: "hexagon", color: "#CC79A7" },
  distributor: { legend: "trader", shape: "ring", color: GREY },
  company: { legend: "trader", shape: "ring", color: GREY },
};

/** Sites of Ma chaîne take the shape of their role. */
export const ROLE_KIND: Record<string, string> = {
  producer: "plant",
  processor: "plant",
  port: "port",
  distributor: "distributor",
  warehouse: "distributor",
};

export const LEGEND: { key: LegendKey; label: string }[] = [
  { key: "plant", label: "Usine" },
  { key: "port", label: "Port" },
  { key: "mine", label: "Mine" },
  { key: "recycler", label: "Recycleur" },
  { key: "passage", label: "Détroit, fleuve" },
  { key: "trader", label: "Distributeur, entreprise" },
  { key: "country", label: "Pays producteur" },
  { key: "flow", label: "Flux" },
  { key: "event", label: "Événement" },
  { key: "zone", label: "Zone d'alerte" },
  { key: "hotspot", label: "Point chaud mondial" },
];

export const EVENT_LOOK: Record<string, { filled: boolean; dashed: boolean; color: string; label: string }> = {
  ongoing: { filled: true, dashed: false, color: VERMILION, label: "en cours" },
  preparing: { filled: false, dashed: true, color: VERMILION, label: "en préparation" },
  early: { filled: false, dashed: true, color: GREY, label: "signal précoce" },
  ended: { filled: false, dashed: false, color: GREY, label: "terminé" },
};

export const MODE_DASH: Record<string, "sea" | "solid" | "dotted"> = {
  sea: "sea",
  river: "sea",
  pipeline: "solid",
  road: "dotted",
  rail: "dotted",
  unknown: "dotted",
};

export function legendOf(nodeType: string): LegendKey | null {
  return KIND[nodeType]?.legend ?? null;
}

export function counts(
  nodes: { node_type: string }[],
  extra: Partial<Record<LegendKey, number>>,
): Record<LegendKey, number> {
  const out = Object.fromEntries(LEGEND.map((l) => [l.key, 0])) as Record<LegendKey, number>;
  for (const n of nodes) {
    const key = legendOf(n.node_type);
    if (key) out[key] += 1;
  }
  for (const [k, v] of Object.entries(extra)) out[k as LegendKey] += v ?? 0;
  return out;
}

const HIDDEN_KEY = "goulot.map.hidden";

/** Legend choices are a per-viewer convenience: storage may be missing or refuse, the map works without it. */
export function loadHidden(): Set<LegendKey> {
  try {
    const raw = window.localStorage.getItem(HIDDEN_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    const known = new Set(LEGEND.map((l) => l.key));
    return new Set(Array.isArray(parsed) ? parsed.filter((k): k is LegendKey => known.has(k as LegendKey)) : []);
  } catch {
    return new Set();
  }
}

export function saveHidden(hidden: Set<LegendKey>): void {
  try {
    window.localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hidden]));
  } catch {
    // private window or blocked storage: the choice lasts for this page only
  }
}

/** Country fill: one blue, more opaque for a larger world share. */
export function countryOpacity(share: number, max: number): number {
  if (max <= 0) return 0.12;
  return Math.round((0.12 + 0.43 * Math.min(share / max, 1)) * 100) / 100;
}
