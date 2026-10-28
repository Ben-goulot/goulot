import { describe, expect, it } from "vitest";
import { dayIndex, marks, VERDICT_COLOR, type Point } from "./adviceMarks";
import type { LedgerAdvice } from "./types";

// Weekdays from Monday 3 August 2026, price 100 then 106 from 18 August.
const points: Point[] = [];
for (let d = new Date("2026-08-03T00:00:00Z"); points.length < 30; d.setUTCDate(d.getUTCDate() + 1)) {
  if (d.getUTCDay() === 0 || d.getUTCDay() === 6) continue;
  const day = d.toISOString().slice(0, 10);
  points.push({ day, value: day >= "2026-08-18" ? 106 : 100 });
}

function advice(a: Partial<LedgerAdvice>): LedgerAdvice {
  return { seq: 1, at: "2026-08-13T12:00:00+00:00", kind: "verdict", outcome: null, ...a };
}

describe("dayIndex", () => {
  it("takes the last point on or before a day, and counts weekdays after the end", () => {
    expect(dayIndex(points, "2026-08-03")).toBe(0);
    expect(dayIndex(points, "2026-08-09")).toBe(4); // Sunday: Friday's point
    expect(dayIndex(points, "2026-08-01")).toBeNull();
    const last = points.length - 1; // Friday 11 September
    expect(points[last].day).toBe("2026-09-11");
    expect(dayIndex(points, "2026-09-15")).toBe(last + 2); // Monday and Tuesday
  });
});

describe("marks", () => {
  const verdict = advice({
    seq: 5,
    verdict: "pot",
    good: true,
    deadline: "2026-09-24",
    outcome: { result: "confirmed", observed_pct: 6, on_index: false, lead_weeks: 0.7 },
  });
  const hypothesis = advice({
    seq: 4,
    kind: "hypothesis",
    horizon: "20d",
    move_p10: 3,
    move_p90: 9,
    deadline: "2026-09-10",
    good: false,
    band: { seq: 4, p10: 3, p90: 9, probability: 0.6, start: "2026-08-13", observed_pct: 6, hit: true },
  });
  const missed = advice({ seq: 9, kind: "missed_move", at: "2026-09-01T00:00:00+00:00", day: "2026-08-18" });
  const m = marks(points, [hypothesis, verdict, missed], ["2026-08-20"], "2026-09-20");

  it("draws a diamond at the issue, in the verdict's color", () => {
    expect(m.diamonds).toEqual([{ seq: 5, index: 8, value: 100, color: VERDICT_COLOR.pot }]);
  });

  it("opens the fan from the price of the day to [p10, p90] at the deadline", () => {
    const [fan] = m.fans;
    expect({ ...fan, low: 0, high: 0 }).toEqual({ seq: 4, from: 8, to: 28, base: 100, low: 0, high: 0, hit: true, pending: false });
    expect(fan.low).toBeCloseTo(103);
    expect(fan.high).toBeCloseTo(109);
  });

  it("puts a confirmed warning's check on the day the threshold was crossed", () => {
    expect(m.results.find((r) => r.seq === 5)).toEqual({ seq: 5, index: 11, value: 106, good: true });
  });

  it("rings a rise nobody announced, marks the version change, runs the ribbon to today", () => {
    expect(m.rings).toEqual([{ seq: 9, index: 11, value: 106 }]);
    expect(m.versions).toEqual([13]);
    // Today (Sunday 20 September) is five weekdays past the last price, Friday 11 September.
    expect(m.ribbon).toEqual([{ from: 8, to: 34, color: VERDICT_COLOR.pot, dashed: false }]);
  });
});
