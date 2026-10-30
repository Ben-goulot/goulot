// Period and day markers of the price chart. Pure functions, tested without a browser.

export type Point = { day: string; value: number };
export type RangeKey = "1W" | "1M" | "6M" | "1Y";
export type Range = { key: RangeKey; label: string; days: number; phrase: string };

export const RANGES: Range[] = [
  { key: "1W", label: "1 semaine", days: 7, phrase: "sur 1 semaine" },
  { key: "1M", label: "1 mois", days: 30, phrase: "sur 1 mois" },
  { key: "6M", label: "6 mois", days: 182, phrase: "sur 6 mois" },
  { key: "1Y", label: "1 an", days: 365, phrase: "sur 1 an" },
];

function daysBefore(day: string, days: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export function slicePoints(points: Point[], days: number): Point[] {
  const last = points[points.length - 1];
  if (!last) return [];
  const start = daysBefore(last.day, days);
  return points.filter((p) => p.day >= start);
}

/** Periods worth a button: at least two points (no « 1 semaine » on a monthly series), and more than a shorter one. */
export function usableRanges(points: Point[]): Range[] {
  const out: Range[] = [];
  let seen = 0;
  for (const r of RANGES) {
    const n = slicePoints(points, r.days).length;
    if (n >= 2 && n > seen) out.push(r);
    seen = Math.max(seen, n);
  }
  return out;
}

export function defaultRange(points: Point[]): Range | null {
  const usable = usableRanges(points);
  return usable.find((r) => r.key === "6M") ?? usable[usable.length - 1] ?? null;
}

export function changePct(points: Point[]): number | null {
  const first = points[0]?.value;
  const last = points[points.length - 1]?.value;
  return first && last ? Math.round(10000 * (last / first - 1)) / 100 : null;
}

export type EventMark = { day: string; letter: string; title: string; retained: boolean };
export type DayMark = { day: string; text: string; retained: boolean; events: EventMark[] };

/** One marker per day of the series. Events of another day of closing move to the next quoted day; those before the
 * period are left out. The retained events are named, the others only counted. */
export function dayMarks(events: EventMark[], points: Point[]): DayMark[] {
  const first = points[0]?.day ?? "";
  const byDay = new Map<string, EventMark[]>();
  for (const e of events) {
    if (e.day < first) continue;
    const day = points.find((p) => p.day >= e.day)?.day;
    if (!day) continue;
    byDay.set(day, [...(byDay.get(day) ?? []), e]);
  }
  return [...byDay.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, list]) => {
      const named = list.filter((e) => e.retained).map((e) => e.letter);
      const others = list.length - named.length;
      const text = named.length ? named.join(" ") + (others ? ` +${others}` : "") : String(others);
      return { day, text, retained: named.length > 0, events: list };
    });
}

/** « 1 sem. » or « 1 à 3 sem. », never « 1 à 1 sem. ». */
export function weeksRange(low: number, high: number): string {
  const a = low.toFixed(0);
  const b = high.toFixed(0);
  return a === b ? `${a} sem.` : `${a} à ${b} sem.`;
}
