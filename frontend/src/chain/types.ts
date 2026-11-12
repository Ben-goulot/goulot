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
  hs_codes: { code: string; cn8?: string; origin: string; verified: boolean }[];
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
  coord_source: CoordSource;
  added_by: "" | "goulot";
  status: SiteStatus;
  share_basis: ShareBasis;
  share_year: number | null;
};

export type SiteStatus = "operating" | "idled" | "closed" | "project" | "unknown";
export type ShareBasis = "production" | "capacity" | "exports" | "unknown";

export const STATUS: Record<SiteStatus, string> = {
  operating: "en service",
  idled: "à l'arrêt",
  closed: "fermé",
  project: "en projet",
  unknown: "non précisé",
};

export const SHARE_BASIS: Record<ShareBasis, string> = {
  production: "de la production mondiale",
  capacity: "de la capacité mondiale",
  exports: "des exportations mondiales",
  unknown: "nature non précisée",
};

/** « 40 % de la capacité mondiale, 2025 »: a share is never shown without what it is a share of. */
export function shareText(n: { global_share_pct: number | null; share_basis: ShareBasis; share_year: number | null }): string {
  if (n.global_share_pct === null) return "";
  const pct = String(n.global_share_pct).replace(".", ",");
  return `${pct} % ${SHARE_BASIS[n.share_basis] ?? SHARE_BASIS.unknown}${n.share_year ? `, ${n.share_year}` : ""}`;
}

export type UpdateItem = {
  id: number;
  change_type: "status" | "share" | "capacity" | "new_producer" | "date" | "note";
  node_key: string;
  summary: string;
  payload: Record<string, unknown>;
  confidence: "high" | "medium" | "low";
  source_refs: string[];
  anomalies: string[];
  decision: "pending" | "accepted" | "rejected";
};

export type ChainUpdate = {
  freshness: {
    researched_at: string | null;
    checked_at: string | null;
    reread_at: string | null;
    age_days: number | null;
    advised: boolean;
    advised_after_days: number;
    pending: number;
    past_dates: { date: string; what: string }[];
    shares_without_basis: number;
  };
  last_update: { kind: "search" | "reread"; created_at: string | null; since: string; notes: string[]; injection_suspected: boolean } | null;
  items: UpdateItem[];
};

export type UpdateQuote = {
  quote_id: string;
  kind: "search" | "reread";
  estimate_usd: number;
  max_usd: number;
  searches_max: number;
  remaining_usd: number;
  cap_usd: number;
};

export const CHANGE_TYPE: Record<UpdateItem["change_type"], string> = {
  status: "État d'un site",
  share: "Part mondiale",
  capacity: "Capacité",
  new_producer: "Producteur à ajouter",
  date: "Échéance",
  note: "À savoir",
};

export type CoordSource = "user" | "ai" | "reference" | "city" | "country" | "none" | "";

/** Where a node's position comes from, from the most to the least precise. A country-level position is approximate. */
export const COORD_SOURCE: Record<CoordSource, string> = {
  user: "placée par toi",
  ai: "donnée par la reconstruction",
  reference: "table de référence (détroits, fleuves)",
  city: "ville du lieu indiqué",
  country: "approximative : centre du pays",
  none: "inconnue",
  "": "pas encore calculée",
};

export function approximate(n: { coord_source: CoordSource }): boolean {
  return n.coord_source === "country" || n.coord_source === "none" || n.coord_source === "";
}

export type ChainEdge = {
  id: number;
  from_key: string;
  to_key: string;
  relation: string;
  transport_mode: string;
  lead_time_days: number | null;
  lead_time_basis: "source" | "sea_estimate" | "default" | "user" | "route";
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
  update: ChainUpdate;
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
  route: "route probable, ajoutée par Goulot",
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
