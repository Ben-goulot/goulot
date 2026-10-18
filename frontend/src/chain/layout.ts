import type { ChainNode } from "./types";

// Columns by role, upstream on the left. Column titles are graph nodes: they pan and zoom with the chain.
export const COLUMNS = ["Matières de base", "Production amont", "Intrants", "Production", "Logistique", "Ta matière"];
const LOGISTICS = new Set(["port", "chokepoint", "waterway", "distributor"]);
export const COL_WIDTH = 250;
export const ROW_HEIGHT = 96;
export const HEADER_Y = -80;

/** Role column of a node, whatever the zoom: a raw material stays with the raw materials. */
export function column(n: ChainNode): number {
  if (n.tier === 0) return 5;
  if (LOGISTICS.has(n.node_type)) return 4;
  if (n.node_type === "material") return n.tier >= 2 ? 0 : 2;
  return n.tier >= 2 ? 1 : 3; // mines, plants, recyclers, producing countries and companies
}

export type Placed = { positions: Map<string, { x: number; y: number }>; headers: { label: string; x: number }[] };

export function layout(nodes: ChainNode[]): Placed {
  const used = [...new Set(nodes.map(column))].sort((a, b) => a - b); // empty columns are not drawn
  const slot = new Map(used.map((c, i) => [c, i]));
  const counts = new Map<number, number>();
  const positions = new Map<string, { x: number; y: number }>();
  const sorted = [...nodes].sort((a, b) => column(a) - column(b) || b.tier - a.tier || a.name.localeCompare(b.name));
  for (const n of sorted) {
    const c = column(n);
    const row = counts.get(c) ?? 0;
    positions.set(n.node_key, { x: (slot.get(c) ?? 0) * COL_WIDTH, y: row * ROW_HEIGHT });
    counts.set(c, row + 1);
  }
  const tallest = Math.max(1, ...counts.values());
  for (const n of sorted) {
    if (column(n) === 5) positions.set(n.node_key, { x: (slot.get(5) ?? 0) * COL_WIDTH, y: ((tallest - 1) * ROW_HEIGHT) / 2 });
  }
  return { positions, headers: used.map((c) => ({ label: COLUMNS[c], x: (slot.get(c) ?? 0) * COL_WIDTH })) };
}
