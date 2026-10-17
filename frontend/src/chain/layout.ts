import type { ChainNode } from "./types";

export const COLUMNS = ["Matières de base", "Intrants", "Production", "Logistique", "Ta matière"];
const LOGISTICS = new Set(["port", "chokepoint", "waterway", "distributor"]);
const PRODUCERS = new Set(["mine", "plant", "recycler", "country", "company", "other"]);
export const COL_WIDTH = 250;
export const ROW_HEIGHT = 96;

/** Column of a node: upstream on the left, the purchased material alone on the right. */
export function column(n: ChainNode): number {
  if (n.tier === 0) return 4;
  if (LOGISTICS.has(n.node_type)) return 3;
  if (n.tier >= 2) return n.node_type === "material" ? 0 : 1;
  return PRODUCERS.has(n.node_type) ? 2 : 1;
}

export function layout(nodes: ChainNode[]): Map<string, { x: number; y: number }> {
  const counts = [0, 0, 0, 0, 0];
  const positions = new Map<string, { x: number; y: number }>();
  const sorted = [...nodes].sort((a, b) => column(a) - column(b) || b.tier - a.tier || a.name.localeCompare(b.name));
  for (const n of sorted) {
    const c = column(n);
    positions.set(n.node_key, { x: c * COL_WIDTH, y: counts[c] * ROW_HEIGHT });
    counts[c] += 1;
  }
  // Center the purchased material vertically against the tallest column.
  const tallest = Math.max(...counts);
  for (const n of sorted) {
    if (column(n) === 4) positions.set(n.node_key, { x: 4 * COL_WIDTH, y: ((tallest - 1) * ROW_HEIGHT) / 2 });
  }
  return positions;
}
