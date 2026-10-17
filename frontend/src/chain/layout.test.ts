import { describe, expect, it } from "vitest";
import { column, layout } from "./layout";
import { clip, safeHref, weeks, type ChainNode } from "./types";

const node = (key: string, type: string, tier: number): ChainNode => ({
  node_key: key, node_type: type, name: key, product: "", tier, country_iso2: "unknown", place: "unknown",
  lat: null, lon: null, global_share_pct: null, buffer_days: null, buffer_is_default: 0, capacity_text: "unknown",
  confidence: "high", notes: "", anomalies: [], user_modified: 0, source_refs: [], delay: null,
});

describe("chain layout", () => {
  it("puts upstream on the left and the purchased material alone on the right", () => {
    expect(column(node("gas", "material", 2))).toBe(0);
    expect(column(node("propylene", "material", 1))).toBe(1);
    expect(column(node("cn", "country", 1))).toBe(2);
    expect(column(node("rotterdam", "port", 1))).toBe(3);
    expect(column(node("acn", "material", 0))).toBe(4);
    const pos = layout([node("a", "country", 1), node("b", "country", 1), node("acn", "material", 0)]);
    expect(pos.get("acn")!.x).toBeGreaterThan(pos.get("a")!.x);
    expect(pos.get("a")!.y).not.toBe(pos.get("b")!.y);
  });
});

describe("display helpers", () => {
  it("links only https URLs", () => {
    expect(safeHref("https://pubchem.ncbi.nlm.nih.gov/compound/7855")).toBeDefined();
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref("http://phish.example/login")).toBeUndefined();
  });
  it("clips model texts at 500 characters and formats weeks", () => {
    expect(clip("x".repeat(600)).length).toBe(501);
    expect(weeks(3)).toBe("3 j");
    expect(weeks(35)).toBe("≈ 5,0 sem.");
  });
});
