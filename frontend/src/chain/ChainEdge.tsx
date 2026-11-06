import {
  BaseEdge,
  getBezierPath,
  Position,
  useStore,
  ViewportPortal,
  type Edge,
  type EdgeProps,
  type InternalNode,
} from "@xyflow/react";
import { useMemo } from "react";
import { LABEL_SIZE, spreadLabels, type Label } from "./hover";

const isLit = (data: unknown) => Boolean((data as { lit?: boolean } | undefined)?.lit);
const textOf = (label: unknown) => (typeof label === "string" || typeof label === "number" ? String(label) : null);

/** The default chain edge; when lit by a hover, its label is drawn by HoverLabels, above the nodes. */
export function ChainEdge(props: EdgeProps) {
  const [path, labelX, labelY] = getBezierPath(props);
  const hide = isLit(props.data) && textOf(props.label) !== null;
  return (
    <BaseEdge
      id={props.id}
      path={path}
      markerEnd={props.markerEnd}
      interactionWidth={props.interactionWidth}
      label={hide ? undefined : props.label}
      labelX={labelX}
      labelY={labelY}
      labelStyle={props.labelStyle}
      labelShowBg={props.labelShowBg}
      labelBgStyle={props.labelBgStyle}
      labelBgPadding={props.labelBgPadding}
      labelBgBorderRadius={props.labelBgBorderRadius}
    />
  );
}

/** Where an edge leaves (right side) or enters (left side) a node, in graph units. */
function anchor(node: InternalNode, side: "source" | "target"): { x: number; y: number } {
  const { x, y } = node.internals.positionAbsolute;
  const handle = node.internals.handleBounds?.[side]?.[0];
  if (handle) return { x: x + handle.x + (side === "source" ? handle.width : 0), y: y + handle.y + handle.height / 2 };
  const w = node.measured.width ?? 0;
  const h = node.measured.height ?? 0;
  return { x: side === "source" ? x + w : x, y: y + h / 2 };
}

/**
 * Labels of the lit edges, above the nodes and spread apart so that none hides another.
 * SVG attributes only, no inline style (CSP); the halo behind the text is an SVG filter.
 */
export function HoverLabels(props: { edges: Edge[]; step: 1 | 2 | 3 | 4 }) {
  const lookup = useStore((s) => s.nodeLookup);
  const size = LABEL_SIZE[props.step];
  const labels = useMemo(() => {
    const raw: Label[] = [];
    for (const e of props.edges) {
      const text = textOf(e.label);
      const from = lookup.get(e.source);
      const to = lookup.get(e.target);
      if (!isLit(e.data) || text === null || !from || !to) continue;
      const s = anchor(from, "source");
      const t = anchor(to, "target");
      const [, x, y] = getBezierPath({
        sourceX: s.x, sourceY: s.y, sourcePosition: Position.Right, targetX: t.x, targetY: t.y, targetPosition: Position.Left,
      });
      raw.push({ id: e.id, x, y, text });
    }
    return spreadLabels(raw, size);
  }, [props.edges, lookup, size]);
  if (labels.length === 0) return null;
  return (
    <ViewportPortal>
      <svg className="edge-lit-layer" aria-hidden="true">
        <defs>
          <filter id="edge-lit-halo" x="-0.06" y="-0.15" width="1.12" height="1.3">
            <feFlood className="edge-lit-halo" />
            <feMerge>
              <feMergeNode />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        {labels.map((l) => (
          <text
            key={l.id}
            className="edge-lit-label"
            x={l.x}
            y={l.y}
            fontSize={size}
            textAnchor="middle"
            dominantBaseline="central"
            filter="url(#edge-lit-halo)"
          >
            {l.text}
          </text>
        ))}
      </svg>
    </ViewportPortal>
  );
}
