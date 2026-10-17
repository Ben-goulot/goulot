// Shapes returned by /api/materials… (phase 2). Model texts are plain strings, always rendered as text.

export type Identity = {
  cas: string;
  name: string;
  name_fr: string;
  formula: string;
  synonyms: string[];
  sources: { name: string; url: string }[];
};

export type Material = {
  id: string;
  name: string;
  cas: string;
  formula: string;
  synonyms: string[];
  hs_codes: { code: string; origin: string; verified: boolean }[];
  identity_sources: { name: string; url: string }[];
  status: "draft" | "reconstructing" | "review" | "validated" | "archived";
};

export type Quote = {
  quote_id: string;
  estimate_usd: number;
  max_usd: number;
  searches_max: number;
  remaining_usd: number;
  cap_usd: number;
};

export type Job = { id: string; step: number; step_label: string; state: "running" | "done" | "failed"; message: string; cost_usd: number };

export type ChainNode = {
  node_key: string;
  node_type: string;
  name: string;
  product: string;
  tier: number;
  country_iso2: string;
  place: string;
  lat: number | null;
  lon: number | null;
  global_share_pct: number | null;
  buffer_days: number | null;
  buffer_is_default: number;
  capacity_text: string;
  confidence: "high" | "medium" | "low";
  notes: string;
  anomalies: string[];
  user_modified: number;
  source_refs: string[];
  delay: { days: number; low: number; high: number } | null;
};

export type ChainEdge = {
  id: number;
  from_key: string;
  to_key: string;
  relation: string;
  transport_mode: string;
  lead_time_days: number | null;
  lead_time_basis: "source" | "sea_estimate" | "default" | "user";
  share_pct: number | null;
  confidence: "high" | "medium" | "low";
  anomalies: string[];
  source_refs: string[];
};

export type Source = { ref: string; url: string; title: string; publisher: string; page_age: string };

export type Chain = {
  material: Material;
  nodes: ChainNode[];
  edges: ChainEdge[];
  sources: Source[];
  routes: { route_key: string; name: string; global_share_pct: number | null; description: string; source_refs: string[] }[];
  risks: { risk_type: string; description: string; source_refs: string[] }[];
  substitutes: { title: string; description: string; source_refs: string[] }[];
  dates: { date: string; what: string; source_refs: string[] }[];
  open_questions: string[];
  injection_suspected: boolean;
};

export const CONFIDENCE: Record<string, string> = { high: "Élevée", medium: "Moyenne", low: "Faible" };

export const NODE_TYPE: Record<string, string> = {
  material: "Matière",
  mine: "Mine ou gisement",
  plant: "Usine",
  recycler: "Recycleur",
  country: "Pays producteur",
  company: "Producteur",
  port: "Port",
  chokepoint: "Détroit ou canal",
  waterway: "Voie fluviale",
  distributor: "Distributeur",
  other: "Autre",
};

export const MODE: Record<string, string> = {
  sea: "mer",
  river: "fleuve",
  rail: "rail",
  road: "route",
  pipeline: "pipeline",
  air: "avion",
  none: "sans transport",
  unknown: "mode inconnu",
};

export const BASIS: Record<string, string> = {
  source: "donné par les sources",
  sea_estimate: "estimé : distance entre ports × 1,3 à 13 nœuds",
  default: "valeur par défaut (hypothèse)",
  user: "corrigé par toi",
};

/** Model text, cut to 500 characters on display (docs/SECURITY.md, AI). */
export function clip(text: string, max = 500): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export function weeks(days: number): string {
  const w = days / 7;
  return w < 1 ? `${Math.round(days)} j` : `≈ ${w.toFixed(w < 10 ? 1 : 0).replace(".", ",")} sem.`;
}

/** Only https links from the code's source table are rendered as links. */
export function safeHref(url: string): string | undefined {
  try {
    const u = new URL(url);
    return u.protocol === "https:" ? u.href : undefined;
  } catch {
    return undefined;
  }
}
