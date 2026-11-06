// Shapes of the compliance report built by the server (goulot/compliance/report.py). Every text from the chain
// (names, risks, sources) comes from the outside: it is rendered as text by React, never as HTML.

export type Option = { id: string; title: string; subtitle: string; version: number };
export type MaterialChoice = { id: string; name: string; cas: string; validated_at: string | null };
export type Options = {
  templates: Option[];
  materials: MaterialChoice[];
  mitigations: { id: string; label: string }[];
};

export type Measures = { choices: string[]; comment: string };
export type Profile = {
  company: string;
  signatory_name: string;
  signatory_role: string;
  measures: Record<string, Measures>;
};

export type Benchmark = { country: string | null; country_name: string | null; share_pct: number | null; status: string };
export type Figures = {
  top_country: string | null;
  top_country_name: string | null;
  top_share_pct: number | null;
  hhi: number | null;
  hhi_band: string | null;
  coverage_pct: number;
  lower_bound: boolean;
  inconsistent: boolean;
  benchmark: Benchmark;
};
export type ConcentrationRow = Figures & { stage: string; stage_label: string; tier: number; products: string[] };
export type Level = {
  tier: number;
  products: string[];
  countries: { country: string; country_name: string; share_pct: number }[];
  sites: { name: string; type: string; country_name: string; place: string; share_pct: number | null; confidence: string; sources: string[] }[];
  confidence: string | null;
  sources: string[];
};
export type Stage = { stage: string; label: string; documented: boolean; levels: Level[] };
export type Risk = { title: string; description: string; sources: string[] };
export type Check = { id: string; label: string; ok: boolean; detail: string };

export type MaterialReport = {
  id: string;
  name: string;
  cas: string;
  chain_validated_at: string | null;
  strategic: { listed: boolean; entry: string | null; source: string };
  mapping: Stage[];
  concentration: ConcentrationRow[];
  most_concentrated: ConcentrationRow | null;
  factors: {
    export_controls: Risk[];
    trade_measures: Risk[];
    chokepoints: { passages: { name: string; type: string; country: string }[]; risks: Risk[] };
    coproduct_dependencies: Risk[];
    other_structural: Risk[];
    events_12_months: { id: string; title: string; place: string; country: string; first_seen: string; status: string; confirmed: boolean }[];
    upcoming_bottlenecks: { date: string; what: string; sources: string[] }[];
  };
  vulnerability: {
    verdict: { verdict: string; label: string; at: string; x_weeks: number | null; x_low: number | null; x_high: number | null } | null;
    propagation: { weeks: number; low_weeks: number; high_weeks: number; buffers_days: number; from: string } | null;
    buffers: { name: string; days: number }[];
    substitutes: Risk[];
    context_documented: boolean;
    alternative_sources: { country: string; country_name: string; share_pct: number }[];
    company_coverage: { status: string; stock_weeks?: number | null; supplier_lead_time_days?: number | null };
  };
  mitigation: { status: string; choices: { id: string; label: string }[]; comment: string };
  my_chain?: {
    sites: { name: string; role: string; company: string; city: string; country_name: string; exposure_pct: number; stock_days: number | null }[];
    concentration: Figures;
  } | null;
  sources: { ref: string; url: string; title: string; publisher: string; date: string }[];
};

export type Report = {
  template: { id: string; version: number; title: string; subtitle: string; reference: string; disclaimer: string; sections: Record<string, string> };
  generated_at: string;
  basis: "typical" | "my_chain";
  confidential: boolean;
  confidential_label: string | null;
  draft: boolean;
  draft_label: string | null;
  company: string;
  signatory: { name: string; role: string };
  reviewed: boolean;
  materials: MaterialReport[];
  method: string;
  reliability: { text: string };
  checks: Check[];
  fingerprint: string;
  ledger_seq?: number;
};

export type ExportResult = {
  report: Report;
  files: { json: { name: string; content: string }; csv: { name: string; content: string } };
};
