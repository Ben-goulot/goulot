import type { Edge, Node } from "@xyflow/react";

/** What the pointer is over in the chain graph. */
export type Hover = { kind: "node" | "edge"; id: string } | null;

type Link = Pick<Edge, "id" | "source" | "target">;

/** Nodes and edges to keep in full light: a node with its direct links and neighbours, or an edge with its two ends. */
export function focus(hover: Hover, edges: Link[]): { nodes: Set<string>; edges: Set<string> } | null {
  if (!hover) return null;
  const nodes = new Set<string>();
  const lit = new Set<string>();
  if (hover.kind === "node") {
    nodes.add(hover.id);
    for (const e of edges) {
      if (e.source !== hover.id && e.target !== hover.id) continue;
      lit.add(e.id);
      nodes.add(e.source);
      nodes.add(e.target);
    }
  } else {
    const e = edges.find((x) => x.id === hover.id);
    if (!e) return null;
    lit.add(e.id);
    nodes.add(e.source);
    nodes.add(e.target);
  }
  return { nodes, edges: lit };
}

const add = (cls: string | undefined, extra: string) => `${cls ?? ""} ${extra}`.trim();

/** Classes and stacking for the hover state; positions never change, so the layout stays put. */
export function emphasize(nodes: Node[], edges: Edge[], hover: Hover): { nodes: Node[]; edges: Edge[] } {
  const lit = focus(hover, edges);
  if (!lit) return { nodes, edges };
  return {
    nodes: nodes.map((n) => {
      if (n.id.startsWith("col:")) return n;
      if (hover?.kind === "node" && n.id === hover.id) return { ...n, className: add(n.className, "hovered"), zIndex: 1000 };
      return { ...n, className: add(n.className, lit.nodes.has(n.id) ? "hover-near" : "hover-dim") };
    }),
    edges: edges.map((e) =>
      lit.edges.has(e.id) ? { ...e, className: add(e.className, "hover-lit"), data: { ...e.data, lit: true } } : { ...e, className: add(e.className, "hover-dim") },
    ),
  };
}

/**
 * Enlargement step for the current zoom: the further out, the bigger the hovered node, so its text stays readable.
 * Steps rather than a computed value: the CSP forbids inline styles.
 */
export function hoverStep(zoom: number): 1 | 2 | 3 | 4 {
  if (zoom >= 0.85) return 1;
  if (zoom >= 0.6) return 2;
  if (zoom >= 0.45) return 3;
  return 4;
}

/** Label size, in graph units, for the hovered state: bigger when zoomed out, to stay readable on screen. */
export const LABEL_SIZE = { 1: 12, 2: 16, 3: 21, 4: 26 } as const;

export type Label = { id: string; x: number; y: number; text: string };

/**
 * Lit edge labels often share a spot (several edges into the same material). Push each one down until it no longer
 * covers another; widths are estimated from the text length.
 */
export function spreadLabels(labels: Label[], fontSize: number): Label[] {
  const height = fontSize * 1.5;
  const width = (t: string) => t.length * fontSize * 0.56 + fontSize;
  const placed: Label[] = [];
  for (const l of [...labels].sort((a, b) => a.y - b.y || a.x - b.x || a.id.localeCompare(b.id))) {
    let y = l.y;
    for (let moved = true; moved; ) {
      moved = false;
      for (const p of placed) {
        if (Math.abs(p.x - l.x) < (width(p.text) + width(l.text)) / 2 && Math.abs(p.y - y) < height) {
          y = p.y + height;
          moved = true;
        }
      }
    }
    placed.push({ ...l, y });
  }
  return placed;
}

const HOVER_KEY = "goulot.chain.hover";

/** The hover effect (enlarged node, dimmed neighbours) can be switched off; a per-viewer convenience kept in the
 * browser. Storage may be missing or refuse: the effect is then on, and a choice lasts for this page only. */
export function loadHoverEffect(): boolean {
  try {
    return window.localStorage.getItem(HOVER_KEY) !== "off";
  } catch {
    return true;
  }
}

export function saveHoverEffect(on: boolean): void {
  try {
    window.localStorage.setItem(HOVER_KEY, on ? "on" : "off");
  } catch {
    // private window or blocked storage
  }
}
