import type { Edge, Node } from "@xyflow/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { emphasize, focus, hoverStep, loadHoverEffect, saveHoverEffect, spreadLabels } from "./hover";

const n = (id: string, className = "chain-node"): Node => ({ id, position: { x: 0, y: 0 }, data: {}, className });
const e = (id: string, source: string, target: string): Edge => ({ id, source, target, className: "chain-edge" });

// gas → cracker → port → acn, and hormuz → acn
const nodes = [n("col:Production", "col-header"), n("gas"), n("cracker"), n("port"), n("hormuz"), n("acn")];
const edges = [e("1", "gas", "cracker"), e("2", "cracker", "port"), e("3", "port", "acn"), e("4", "hormuz", "acn")];

describe("chain hover", () => {
  it("lights a node, its links and its direct neighbours only", () => {
    const lit = focus({ kind: "node", id: "acn" }, edges)!;
    expect([...lit.nodes].sort()).toEqual(["acn", "hormuz", "port"]);
    expect([...lit.edges].sort()).toEqual(["3", "4"]);
  });
  it("lights an edge and its two ends", () => {
    const lit = focus({ kind: "edge", id: "2" }, edges)!;
    expect([...lit.nodes].sort()).toEqual(["cracker", "port"]);
    expect([...lit.edges]).toEqual(["2"]);
    expect(focus({ kind: "edge", id: "missing" }, edges)).toBeNull();
    expect(focus(null, edges)).toBeNull();
  });
  it("enlarges the hovered node and dims the rest without moving anything", () => {
    const out = emphasize(nodes, edges, { kind: "node", id: "port" });
    const by = new Map(out.nodes.map((x) => [x.id, x]));
    expect(by.get("port")!.className).toBe("chain-node hovered");
    expect(by.get("port")!.zIndex).toBeGreaterThan(0);
    expect(by.get("acn")!.className).toBe("chain-node hover-near");
    expect(by.get("gas")!.className).toBe("chain-node hover-dim");
    expect(by.get("col:Production")!.className).toBe("col-header"); // column titles untouched
    expect(out.nodes.map((x) => x.position)).toEqual(nodes.map((x) => x.position));
    const lit = out.edges.filter((x) => x.className?.includes("hover-lit")).map((x) => x.id);
    expect(lit.sort()).toEqual(["2", "3"]);
    expect(out.edges.find((x) => x.id === "3")!.data).toEqual({ lit: true }); // its label goes above the nodes
    expect(out.edges.find((x) => x.id === "4")!.className).toBe("chain-edge hover-dim");
  });
  it("leaves the graph as it is without hover", () => {
    const out = emphasize(nodes, edges, null);
    expect(out.nodes).toBe(nodes);
    expect(out.edges).toBe(edges);
  });
  it("enlarges more when zoomed out", () => {
    expect(hoverStep(1.2)).toBe(1);
    expect(hoverStep(0.7)).toBe(2);
    expect(hoverStep(0.5)).toBe(3);
    expect(hoverStep(0.2)).toBe(4);
  });
  it("spreads lit labels that share a spot, and leaves apart ones alone", () => {
    const same = "mode inconnu · ≈ 1,0 sem.";
    const out = spreadLabels([
      { id: "a", x: 900, y: 300, text: same },
      { id: "b", x: 905, y: 302, text: "mer · ≈ 4,3 sem." },
      { id: "c", x: 910, y: 304, text: same },
      { id: "far", x: 100, y: 300, text: same },
    ], 12);
    const y = new Map(out.map((l) => [l.id, l.y]));
    expect(y.get("far")).toBe(300);
    const near = ["a", "b", "c"].map((id) => y.get(id)!).sort((p, q) => p - q);
    expect(near[1] - near[0]).toBeGreaterThanOrEqual(18);
    expect(near[2] - near[1]).toBeGreaterThanOrEqual(18);
    expect(out.map((l) => l.x).sort()).toEqual([100, 900, 905, 910]); // only moved down, never sideways
  });
});

describe("hover effect switch", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("is on by default, remembers « off », and survives blocked storage", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("window", { localStorage: { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => store.set(k, v) } });
    expect(loadHoverEffect()).toBe(true);
    saveHoverEffect(false);
    expect(loadHoverEffect()).toBe(false);
    saveHoverEffect(true);
    expect(loadHoverEffect()).toBe(true);
    vi.stubGlobal("window", { localStorage: { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } } });
    expect(() => saveHoverEffect(false)).not.toThrow();
    expect(loadHoverEffect()).toBe(true);
  });
});
