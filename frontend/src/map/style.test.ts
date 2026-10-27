import { afterEach, describe, expect, it, vi } from "vitest";
import { counts, countryOpacity, EVENT_LOOK, KIND, LEGEND, loadHidden, saveHidden, VERMILION } from "./style";

describe("map conventions", () => {
  it("gives every type its own shape, so color is never the only code", () => {
    const shapes = new Map<string, string>();
    for (const look of Object.values(KIND)) {
      const owner = shapes.get(look.shape);
      expect(owner === undefined || owner === look.legend).toBe(true);
      shapes.set(look.shape, look.legend);
    }
    expect(Object.values(KIND).every((k) => k.color !== VERMILION)).toBe(true); // vermilion is for alerts
  });
  it("tells event states apart without color", () => {
    expect(EVENT_LOOK.ongoing.filled).toBe(true);
    expect(EVENT_LOOK.preparing.filled || !EVENT_LOOK.preparing.dashed).toBe(false);
    expect(EVENT_LOOK.ended.dashed).toBe(false);
  });
  it("counts each legend entry", () => {
    const c = counts([{ node_type: "plant" }, { node_type: "chokepoint" }, { node_type: "waterway" }, { node_type: "material" }], { flow: 3 });
    expect(c.plant).toBe(1);
    expect(c.passage).toBe(2);
    expect(c.flow).toBe(3);
    expect(Object.keys(c)).toHaveLength(LEGEND.length);
  });
  it("shades countries by world share", () => {
    expect(countryOpacity(40, 40)).toBe(0.55);
    expect(countryOpacity(0, 40)).toBe(0.12);
    expect(countryOpacity(5, 0)).toBe(0.12);
  });
  afterEach(() => vi.unstubAllGlobals());
  it("keeps legend choices when storage works, and survives when it does not", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("window", { localStorage: { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => store.set(k, v) } });
    saveHidden(new Set(["flow", "zone"]));
    expect([...loadHidden()].sort()).toEqual(["flow", "zone"]);
    store.set("goulot.map.hidden", "{broken");
    expect(loadHidden().size).toBe(0);
    vi.stubGlobal("window", { localStorage: { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } } });
    expect(() => saveHidden(new Set(["flow"]))).not.toThrow();
    expect(loadHidden().size).toBe(0);
  });
});
