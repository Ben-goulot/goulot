// Ma chaîne and the personal verdict (phase 4). These shapes come only from /api/private/, vault open; they are
// kept in memory while the vault is open and dropped as soon as it closes.

export type Alternative = { name: string; status: "qualified" | "qualifying" | "not_qualified"; switch_weeks: number | null };

export type Site = {
  id: string;
  name: string;
  role: "producer" | "processor" | "distributor" | "warehouse" | "port";
  company: string;
  aliases: string[];
  city: string;
  country_iso2: string;
  lat: number | null;
  lon: number | null;
  node_key: string | null;
  watch_level: "hazards" | "city_material" | "city_material_company";
  stock_days: number | null;
  alternatives: Alternative[];
  notes: string;
};

export type Link = {
  source: string;
  target: string; // a site id, or "me"
  share_pct: number;
  transport_mode: string;
  incoterm: string;
  lead_days: number | null;
};

export type MyChain = {
  sites: Site[];
  links: Link[];
  not_used: string[];
  stock_at_user_days: number | null;
  stock_in_transit_days: number | null;
  notes: string;
};

export type Concentration = { top_country: string | null; top_share_pct: number | null; hhi: number | null };

export type Described = {
  check: { errors: string[]; warnings: string[] };
  flows?: { source: string; target: string; flow_pct: number }[];
  sites?: Record<string, { exposure_pct: number; weighs: boolean; unexplained_pct: number }>;
  concentration?: { market: Concentration; mine: Concentration };
};

export type MyChainResponse = Described & {
  revision: number;
  revisions: { revision: number; created_at: string }[];
  chain: MyChain;
  roles: Record<string, string>;
  watch_levels: Record<string, string>;
  cities_available: boolean;
  attribution: string;
  ready: boolean;
};

export type City = { name: string; country_iso2: string; lat: number; lon: number; population: number };

export type PersonalVerdict = {
  id: string;
  verdict: "ras" | "pot" | "ant" | "unverified";
  pressure: number;
  break_in: number;
  created_at: string;
  label?: string;
  x_weeks?: number | null;
  x_low?: number | null;
  x_high?: number | null;
  window_weeks?: number | null;
  window_assumed?: boolean;
  explanation?: string;
  event_ids?: string[];
  exposures?: Record<string, { exposure_pct: number; s: number; s_perso: number; on_path: boolean; x_weeks: number | null; sites: string[]; title: string; public: boolean }>;
  plan_b?: { site: string; name: string; status: string; switch_weeks: number | null }[];
  threshold_pct?: number;
  revision?: number | null;
};

export type Signal = {
  id: string;
  type: string;
  label: string;
  site_id: string | null;
  site: string;
  date: string;
  effective_date: string | null;
  pct: number | null;
  old_lead_days: number | null;
  new_lead_days: number | null;
  comment: string;
  active: boolean;
  expired: boolean;
};

export type PriceRecord = { id: string; date: string; unit_price: number; currency: string; unit: string; quantity: number | null };

export type Question = { seq: number; material_id: string; at: string; label: string; window_end: string };

export type Personal = {
  verdict: PersonalVerdict | null;
  coverage: { stock_weeks: number | null; supplier_lead_time_days: number | null; price_threshold_pct: number | null } | null;
  links: Record<string, { sites: string[]; how: string[]; site_names: string[] }>;
  private_events: { id: string; title: string; place: string; event_status: string; confirmation: string; first_seen_at: string }[];
  signals: Signal[];
  quotes: PriceRecord[];
  purchases: PriceRecord[];
  questions: Question[];
};

export const SIGNAL_LABELS: Record<string, string> = {
  delay: "Délai allongé",
  price_increase: "Hausse annoncée",
  allocation: "Allocation",
  force_majeure: "Force majeure",
  rupture: "Rupture",
  back_to_normal: "Retour à la normale",
};

export const WATCH_HELP: Record<string, string> = {
  hazards: "Rien ne part du Mac : seuls les aléas naturels proches (50 km) sont comparés ici.",
  city_material: "La ville et la matière partent dans les recherches, jamais le nom du site ni le fait qu'il est ton fournisseur.",
  city_material_company: "La ville, la matière et le nom de l'entreprise partent dans les recherches.",
};

export function emptyChain(): MyChain {
  return { sites: [], links: [], not_used: [], stock_at_user_days: null, stock_in_transit_days: null, notes: "" };
}

export function newSiteId(chain: MyChain, name: string): string {
  const base =
    name
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 30) || "site";
  let id = base;
  let n = 2;
  while (chain.sites.some((s) => s.id === id) || id === "me") id = `${base}-${n++}`;
  return id;
}

export function fmtPct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value.toLocaleString("fr-FR", { maximumFractionDigits: 1 })} %`;
}

export function fmtWeeks(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value.toLocaleString("fr-FR", { maximumFractionDigits: 1 })} sem.`;
}
