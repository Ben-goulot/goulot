import { describe, expect, it } from "vitest";
import { CENSUS_NOTICE, RANK_LABEL, RESULT_LABEL, scale, signed, sourceCredit, verdictClass, watchLabel, type Verdict } from "./types";

const verdict = (v: Verdict["verdict"]) => ({ verdict: v }) as Verdict;

describe("watch display helpers", () => {
  it("always signs variations, with a French decimal comma", () => {
    expect(signed(4.56)).toBe("+4,6");
    expect(signed(-2)).toBe("-2,0");
    expect(signed(null)).toBe("—");
  });
  it("gives each verdict its own class, never color alone", () => {
    expect(verdictClass(verdict("ant"))).toBe("badge ant");
    expect(verdictClass(verdict("pot"))).toBe("badge pot");
    expect(verdictClass(verdict("unverified"))).toBe("badge unverified");
    expect(verdictClass(null)).toBe("badge neutral");
  });
  it("names every series rank and outcome in French", () => {
    expect(Object.keys(RANK_LABEL)).toHaveLength(6);
    expect(RESULT_LABEL.unresolved).toBe("non résolu");
  });
});

describe("scale of the comparable cases card", () => {
  it("always shows zero and keeps the order of the values", () => {
    const at = scale([4, 10, -2]);
    expect(at(-2)).toBeLessThan(at(0));
    expect(at(0)).toBeLessThan(at(4));
    expect(at(10)).toBeLessThan(1);
    expect(at(-2)).toBeGreaterThan(0);
    const positive = scale([3, 5]);
    expect(positive(0)).toBeGreaterThan(0); // zero is inside the axis even when every case rose
  });
});

describe("source credits (terms of service)", () => {
  it("shows the Census notice verbatim and never presents a Goulot index as published data", () => {
    expect(CENSUS_NOTICE).toBe("This product uses the Census Bureau Data API but is not endorsed or certified by the Census Bureau.");
    expect(sourceCredit("us_census_trade")).toContain(CENSUS_NOTICE);
    expect(sourceCredit("us_census_trade")).toContain("calculé par Goulot");
    expect(sourceCredit("eia")).toContain("U.S. Energy Information Administration");
    expect(sourceCredit("goulot")).toContain("calculé par Goulot");
    expect(sourceCredit(undefined)).toBeNull();
  });
});

describe("watch state of a material", () => {
  it("says plainly when a material is not watched", () => {
    expect(watchLabel({ state: "chain_to_validate", last_cycle: null, hours_ago: null })).toEqual({
      text: "Chaîne à valider : pas de veille",
      warn: true,
    });
  });
  it("gives the age of the last watch, and warns when it is late", () => {
    expect(watchLabel({ state: "active", last_cycle: "x", hours_ago: 2 }).text).toBe("Veille active · dernière veille il y a 2 h");
    expect(watchLabel({ state: "active", last_cycle: "x", hours_ago: 0 }).text).toContain("moins d'1 h");
    expect(watchLabel({ state: "late", last_cycle: "x", hours_ago: 15 }).warn).toBe(true);
  });
});
