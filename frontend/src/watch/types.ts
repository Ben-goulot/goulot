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
  points: { day: string; value: number }[];
  change_pct: number | null;
};

export type Freshness = { last_ok_cycle: string | null; verified: boolean };

export type MarketRow = {
  id: string;
  name: string;
  cas: string;
  status: string;
  verdict: Verdict | null;
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
};

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
