import { describe, expect, it } from "vitest";
import { changePct, dayMarks, defaultRange, slicePoints, usableRanges, weeksRange, type Point } from "./chartView";

function daily(from: string, count: number, step = 1): Point[] {
  const out: Point[] = [];
  const d = new Date(`${from}T00:00:00Z`);
  for (let i = 0; i < count; i++) {
    out.push({ day: d.toISOString().slice(0, 10), value: 100 + i });
    d.setUTCDate(d.getUTCDate() + step);
  }
  return out;
}

describe("périodes du graphique", () => {
  const year = daily("2025-10-29", 366);

  it("découpe la période à partir du dernier jour", () => {
    expect(slicePoints(year, 7)).toHaveLength(8);
    expect(slicePoints(year, 7)[0].day).toBe("2026-10-22");
    expect(slicePoints([], 7)).toEqual([]);
  });

  it("propose les quatre périodes pour une série quotidienne d'un an", () => {
    expect(usableRanges(year).map((r) => r.key)).toEqual(["1W", "1M", "6M", "1Y"]);
    expect(defaultRange(year)?.key).toBe("6M");
  });

  it("ne propose pas une semaine sur une série mensuelle", () => {
    const monthly = daily("2025-11-01", 12, 30);
    expect(usableRanges(monthly).map((r) => r.key)).toEqual(["1M", "6M", "1Y"]);
  });

  it("n'ajoute pas une période qui montrerait les mêmes points", () => {
    const short = daily("2026-10-01", 20);
    expect(usableRanges(short).map((r) => r.key)).toEqual(["1W", "1M"]);
    expect(defaultRange(short)?.key).toBe("1M");
    expect(defaultRange(daily("2026-10-01", 1))).toBeNull();
  });

  it("calcule la variation de la période affichée", () => {
    expect(changePct([{ day: "a", value: 100 }, { day: "b", value: 113.1 }])).toBe(13.1);
    expect(changePct([])).toBeNull();
  });
});

describe("repères du graphique", () => {
  const points = daily("2026-10-26", 4); // 26, 27, 28, 29
  const event = (letter: string, day: string, retained = false) => ({ day, letter, title: `Événement ${letter}`, retained });

  it("regroupe les événements d'un même jour en un seul repère", () => {
    const marks = dayMarks(["A", "B", "C", "R", "S"].map((l) => event(l, "2026-10-28")), points);
    expect(marks).toHaveLength(1);
    expect(marks[0]).toMatchObject({ day: "2026-10-28", text: "5", retained: false });
    expect(marks[0].events).toHaveLength(5);
  });

  it("nomme seulement les événements retenus par le verdict", () => {
    const marks = dayMarks([event("A", "2026-10-28"), event("B", "2026-10-28", true), event("C", "2026-10-28")], points);
    expect(marks[0]).toMatchObject({ text: "B +2", retained: true });
  });

  it("place un repère sur le jour coté suivant et écarte ceux d'avant la période", () => {
    const gap = [{ day: "2026-10-23", value: 1 }, { day: "2026-10-26", value: 2 }];
    expect(dayMarks([event("A", "2026-10-24")], gap)[0].day).toBe("2026-10-26");
    expect(dayMarks([event("A", "2026-10-01")], gap)).toEqual([]);
    expect(dayMarks([event("A", "2026-11-01")], gap)).toEqual([]);
  });
});

describe("délais", () => {
  it("n'écrit jamais « 1 à 1 sem. »", () => {
    expect(weeksRange(1.2, 0.9)).toBe("1 sem.");
    expect(weeksRange(1, 3)).toBe("1 à 3 sem.");
  });
});
