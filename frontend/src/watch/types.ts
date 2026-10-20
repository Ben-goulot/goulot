// Shapes returned by /api/markets, /api/materials/{id}/sheet and /api/reliability. Texts from sources or from
// the model are plain strings: always rendered as text, clipped with `clip`.

export type Verdict = {
  verdict: "ras" | "pot" | "ant" | "unverified";
  label: string;
  pressure: number;
  pressure_label: string;
  x_weeks: number | null;
  x_low: number | null;
  x_high: number | null;
  window_weeks: number | null;
  window_assumed: number;
  rules_only: number;
  break_in: number;
  explanation: string;
  created_at: string;
  event_ids: string[];
};

export type SeriesPayload = {
  name: string;
  rank: number;
  frequency: string;
  source?: string;
  points: { day: string; value: number }[];
  change_pct: number | null;
};

// Attribution required by the terms of service of each source (EIA, Census Bureau API), and the line that says when
// a figure is computed by Goulot rather than published by the source.
export const CENSUS_NOTICE = "This product uses the Census Bureau Data API but is not endorsed or certified by the Census Bureau.";

export const SOURCE_CREDIT: Record<string, string> = {
  eia: "Source : U.S. Energy Information Administration (EIA).",
  us_census_trade: "Prix unitaire calculé par Goulot (valeur ÷ quantité) à partir des données du Census Bureau. " + CENSUS_NOTICE,
  nbs_china_prices: "Source : Bureau national des statistiques de Chine (prix de marché des moyens de production).",
  shfe_ine: "Source : Shanghai Futures Exchange (prix de compensation du contrat le plus traité, choisi par Goulot).",
  czce: "Source : Zhengzhou Commodity Exchange (prix de compensation du contrat le plus traité, choisi par Goulot).",
  bls_ppi: "Source : U.S. Bureau of Labor Statistics.",
  eurostat: "Source : Eurostat.",
  fred: "Source : FRED, Federal Reserve Bank of St. Louis.",
  yahoo_chart: "Source : Yahoo Finance (dépannage, usage personnel).",
  stooq: "Source : Stooq (dépannage, usage personnel).",
  goulot: "Indice calculé par Goulot à partir des séries liées ; ce n'est pas une donnée publiée par une source.",
};

export function sourceCredit(source: string | undefined): string | null {
  return source ? SOURCE_CREDIT[source] ?? null : null;
}

export type Freshness = { last_ok_cycle: string | null; verified: boolean };

/** What a closed vault shows of a personal verdict: no X, « motif dans le coffre ». */
export type LockedPersonal = {
  verdict: "ras" | "pot" | "ant" | "unverified";
  label: string;
  window_weeks: number | null;
  reason: string;
};

export type MarketRow = {
  id: string;
  name: string;
  cas: string;
  status: string;
  verdict: Verdict | null;
  personal: LockedPersonal | null;
  series: SeriesPayload | null;
};

export type SheetEvent = {
  id: string;
  letter: string;
  title: string;
  place: string;
  status_label: string;
  status: "ongoing" | "preparing" | "early";
  c_fait: number;
  lineages: number;
  nodes: string[];
  publishers: string[];
  first_seen: string;
  lat: number | null;
  lon: number | null;
  analysis_state: string;
  impact: { p10: number; p90: number; probability: number; unit: string } | null;
  headline: string;
};

export type Sheet = {
  material: { id: string; name: string; cas: string; status: string; hs_codes: { code: string }[] };
  verdict: Verdict | null;
  personal: LockedPersonal | null;
  freshness: Freshness;
  events: SheetEvent[];
  analysis: {
    headline: string;
    summary: string;
    triggers: { indicator: string; signal: string; implication: string }[];
    confidence: number | null;
  } | null;
  series: SeriesPayload | null;
  links: { instrument_id: string; name: string; role: string; rank: number; validated: boolean; proposed_by: string }[];
  upcoming: { date: string; what: string; source: string }[];
  sites: { name: string; lat: number; lon: number; type: string }[];
  margin: { trend: "up" | "down" | "flat"; change_pct_20: number; series: SeriesPayload | null } | null;
};

export type CaseSource = { ref: string; url: string; title: string; publisher: string; page_age: string; reliability: number };

export type CaseReaction = {
  instrument_id: string;
  name: string;
  frequency: string;
  base_day: string;
  raw: Record<string, number | null>;
  own: Record<string, number | null>;
  beta: number | null;
  factor: string | null;
  notes: string[];
};

export type PastCase = {
  id: number;
  case_id: string;
  title: string;
  event_type: string;
  first_report_date: string;
  end_date: string;
  place: string;
  country_iso2: string;
  node_type: string;
  affected: string[];
  capacity_share_pct: number | null;
  capacity_share_basis: string;
  direction: "tightening" | "loosening" | "ambiguous";
  duration_days: number | null;
  summary: string;
  confidence: "high" | "medium" | "low";
  status: "draft" | "verified" | "rejected";
  anomalies: string[];
  sources: CaseSource[];
  overlap: boolean;
  reactions: CaseReaction[];
};

export type CaseLibrary = {
  material: { id: string; name: string; cas: string };
  cases: PastCase[];
  summaries: {
    instrument_id: string;
    name: string;
    horizon: number;
    n: number;
    median: number | null;
    low: number | null;
    high: number | null;
    years: number[];
    excluded_overlap: number;
  }[];
  job: { id: string; state: string; step: number; message: string; cost_usd: number } | null;
};

export type ComparableRow = PastCase & { points: number; state: "kept" | "overlap" | "draft"; own_20: number | null };

export type Comparables = {
  event: { id: string; title: string; type: string; first_seen: string };
  summary: string;
  instrument_id: string | null;
  rows: ComparableRow[];
  band: { low: number; high: number; median: number } | null;
  estimate: { low: number; high: number; unit: string } | null;
  estimate_outside: boolean;
  threshold: number;
  as_of: string;
};

/** x position (0 to 1) of each value on a common scale that always shows zero. */
export function scale(values: number[]): (v: number) => number {
  const lo = Math.min(0, ...values);
  const hi = Math.max(0, ...values);
  const pad = Math.max(1, (hi - lo) * 0.1);
  return (v: number) => (v - (lo - pad)) / (hi - lo + 2 * pad);
}

export type LedgerAdvice = {
  seq: number;
  at: string;
  kind: string;
  verdict?: string;
  label?: string;
  pressure?: number;
  x_weeks?: number | null;
  instrument_id?: string;
  horizon?: string;
  move_p10?: number;
  move_p90?: number;
  probability?: number;
  day?: string;
  outcome: { result: string; observed_pct: number | null; on_index: boolean; lead_weeks: number | null } | null;
};

export type Reliability = {
  ledger: { ok: boolean; entries: number; head: string; problem: string; at_seq: number | null };
  indicators: {
    break_in: boolean;
    since: string | null;
    warnings_confirmed: { good: number; resolved: number; matured: number; on_index: number };
    false_alarms: number;
    calm_right: { good: number; resolved: number; matured: number; on_index: number };
    missed_rises: number;
    pressure_right: { good: number; resolved: number; matured: number; on_index: number };
    ranges_hit: { good: number; resolved: number; matured: number; on_index: number };
    average_lead_weeks: number | null;
    resolved: number;
  };
  advices: LedgerAdvice[];
  series: SeriesPayload | null;
};

export const RANK_LABEL: Record<number, string> = {
  1: "cotation",
  2: "prix officiel",
  3: "prix douanier",
  4: "indice",
  5: "indice des intrants",
  6: "dépannage",
};

export const RESULT_LABEL: Record<string, string> = {
  confirmed: "confirmé",
  false_alarm: "fausse alerte",
  correct_calm: "calme juste",
  missed_rise: "hausse manquée",
  range_hit: "fourchette juste",
  range_missed: "fourchette manquée",
  right_direction: "bon sens",
  wrong_direction: "mauvais sens",
  no_effect: "sans effet",
  unresolved: "non résolu",
};

export function verdictClass(v: Verdict | null): string {
  if (!v) return "badge neutral";
  return { ant: "badge ant", pot: "badge pot", ras: "badge neutral", unverified: "badge unverified" }[v.verdict];
}

export function signed(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined) return "—";
  const s = n.toFixed(digits).replace(".", ",");
  return n > 0 ? `+${s}` : s;
}

export const EVENT_TYPE_LABEL: Record<string, string> = {
  labor_action: "grève",
  armed_conflict: "conflit armé",
  civil_unrest: "troubles civils",
  extreme_weather: "météo extrême",
  natural_disaster: "catastrophe naturelle",
  industrial_outage: "arrêt industriel",
  maritime_disruption: "perturbation maritime",
  infrastructure_failure: "défaillance d'infrastructure",
  sanctions_trade_policy: "sanction ou mesure commerciale",
  export_restriction: "restriction d'exportation",
  regulatory_change: "changement réglementaire",
  cyber_incident: "cyberattaque",
  health_biosecurity: "crise sanitaire",
  other: "autre",
};

export const CONFIDENCE_LABEL: Record<string, string> = { high: "élevée", medium: "moyenne", low: "faible" };
