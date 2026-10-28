// What the Fiabilité chart draws over the real price (docs/SPEC.md, section 8): a diamond where an advice was issued,
// its p10–p90 range as a fan up to its deadline, a check or a cross where its outcome was decided, a ring at each
// rise nobody announced, the ribbon of verdicts under the curve, a dashed line at each new model or prompt version.
// Pure functions: the drawing is in AdviceChart.tsx. Shape and glyph always say what color says.

import type { LedgerAdvice } from "./types";

export const VERDICT_COLOR: Record<string, string> = {
  ant: "#FF8A4C",
  pot: "#F5B841",
  ras: "#3A4656",
  unverified: "#5C6B7E",
};
// The ribbon's grey is too dark for a marker on the curve: the diamond of « Rien à signaler » is lighter.
const DIAMOND_COLOR: Record<string, string> = { ...VERDICT_COLOR, ras: "#B8C2CF" };
export const GOOD = "#3CCB91";
export const BAD = "#FF7B7B";
export const FAN = "#F5B841";

export type Point = { day: string; value: number };

/** Logical index of a day on the series: the last point on or before it; after the end, counted in weekdays. */
export function dayIndex(points: Point[], day: string): number | null {
  if (points.length === 0 || day < points[0].day) return null;
  const last = points[points.length - 1];
  if (day <= last.day) {
    let lo = 0;
    let hi = points.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (points[mid].day <= day) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }
  let extra = 0;
  const d = new Date(`${last.day}T00:00:00Z`);
  const end = new Date(`${day}T00:00:00Z`);
  while (d < end) {
    d.setUTCDate(d.getUTCDate() + 1);
    const wd = d.getUTCDay();
    if (wd !== 0 && wd !== 6) extra += 1;
  }
  return points.length - 1 + extra;
}

export function valueAt(points: Point[], day: string): number | null {
  const i = dayIndex(points, day);
  return i === null || i >= points.length ? null : points[i].value;
}

export function addDays(day: string, days: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export type Fan = { seq: number; from: number; to: number; base: number; low: number; high: number; hit: boolean | null; pending: boolean };
export type Diamond = { seq: number; index: number; value: number; color: string };
export type Result = { seq: number; index: number; value: number; good: boolean };
export type Ring = { seq: number; index: number; value: number };
export type Segment = { from: number; to: number; color: string; dashed: boolean };
export type Marks = { fans: Fan[]; diamonds: Diamond[]; results: Result[]; rings: Ring[]; ribbon: Segment[]; versions: number[] };

/** Where the outcome of an advice was decided: the day the threshold was crossed, or its deadline. */
function decidedOn(a: LedgerAdvice): string | null {
  const o = a.outcome;
  if (!o || o.result === "unresolved") return null;
  if (o.result === "confirmed" && o.lead_weeks !== null) return addDays(a.at.slice(0, 10), Math.round(o.lead_weeks * 7));
  return a.deadline ?? null;
}

export function marks(points: Point[], advices: LedgerAdvice[], versionDays: string[], today: string): Marks {
  const out: Marks = { fans: [], diamonds: [], results: [], rings: [], ribbon: [], versions: [] };
  const lastIndex = points.length - 1;
  for (const a of advices) {
    const day = a.at.slice(0, 10);
    const index = dayIndex(points, day);
    if (a.kind === "missed_move" && a.day) {
      const i = dayIndex(points, a.day);
      const v = valueAt(points, a.day);
      if (i !== null && v !== null) out.rings.push({ seq: a.seq, index: i, value: v });
      continue;
    }
    if (index === null || index > lastIndex) continue;
    const base = points[index].value;
    if (a.kind === "hypothesis" && a.horizon === "20d" && a.deadline && a.move_p10 !== undefined && a.move_p90 !== undefined) {
      const to = dayIndex(points, a.deadline);
      if (to !== null && to > index)
        out.fans.push({
          seq: a.seq,
          from: index,
          to,
          base,
          low: base * (1 + a.move_p10 / 100),
          high: base * (1 + a.move_p90 / 100),
          hit: a.band?.hit ?? null,
          pending: !a.outcome && a.deadline > today,
        });
    }
    if (a.kind === "verdict" && a.verdict && a.verdict !== "unverified")
      out.diamonds.push({ seq: a.seq, index, value: base, color: DIAMOND_COLOR[a.verdict] ?? DIAMOND_COLOR.ras });
    // Results of verdicts and of the 20-session ranges drawn as fans; shorter horizons stay in the table.
    const drawn = a.kind === "verdict" || (a.kind === "hypothesis" && a.horizon === "20d");
    if (drawn && a.good !== null && a.good !== undefined) {
      const on = decidedOn(a);
      const i = on ? dayIndex(points, on) : null;
      const v = on ? valueAt(points, on) : null;
      if (i !== null && v !== null && i <= lastIndex) out.results.push({ seq: a.seq, index: i, value: v, good: a.good });
    }
  }
  const verdicts = advices.filter((a) => a.kind === "verdict" && a.verdict);
  const end = dayIndex(points, today) ?? lastIndex;
  verdicts.forEach((a, k) => {
    const from = dayIndex(points, a.at.slice(0, 10)) ?? 0;
    const next = verdicts[k + 1];
    const to = next ? dayIndex(points, next.at.slice(0, 10)) ?? from : Math.max(end, from);
    if (to >= from && to >= 0)
      out.ribbon.push({ from: Math.max(from, 0), to, color: VERDICT_COLOR[a.verdict ?? "ras"] ?? VERDICT_COLOR.ras, dashed: a.verdict === "unverified" });
  });
  for (const v of versionDays) {
    const i = dayIndex(points, v);
    if (i !== null) out.versions.push(i);
  }
  return out;
}
