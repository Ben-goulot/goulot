import {
  createChart,
  LineSeries,
  type AutoscaleInfo,
  type IChartApi,
  type ISeriesApi,
  type IPrimitivePaneRenderer,
  type IPrimitivePaneView,
  type ISeriesPrimitive,
  type Logical,
  type MouseEventParams,
  type SeriesAttachedParameter,
  type Time,
} from "lightweight-charts";
import { useEffect, useMemo, useRef } from "react";
import { BAD, FAN, GOOD, marks, type Marks, type Point } from "./adviceMarks";
import { sourceCredit, type LedgerAdvice, type SeriesPayload } from "./types";

type Scope = { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } };
type Target = { useMediaCoordinateSpace<T>(f: (scope: Scope) => T): T };
type Hit = { seq: number; x: number; y: number };

const RIBBON = 7;

/** Draws the advices over the price line (see adviceMarks.ts) and remembers where, for clicks. */
class AdvicePrimitive implements ISeriesPrimitive<Time> {
  data: Marks = { fans: [], diamonds: [], results: [], rings: [], ribbon: [], versions: [] };
  selected: number[] = [];
  hits: Hit[] = [];
  private chart: SeriesAttachedParameter<Time>["chart"] | null = null;
  private series: ISeriesApi<"Line"> | null = null;
  private request: (() => void) | null = null;
  private view: IPrimitivePaneView;

  constructor() {
    const renderer: IPrimitivePaneRenderer = { draw: (target) => this.draw(target as unknown as Target) };
    this.view = { zOrder: () => "top", renderer: () => renderer };
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.series = param.series as ISeriesApi<"Line">;
    this.request = param.requestUpdate;
  }

  detached(): void {
    this.chart = this.series = this.request = null;
  }

  set(data: Marks, selected: number[]): void {
    this.data = data;
    this.selected = selected;
    this.request?.();
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [this.view];
  }

  /** The fans count in the vertical range, or a wide one would be cut off. */
  autoscaleInfo(start: Logical, end: Logical): AutoscaleInfo | null {
    const seen = this.data.fans.filter((f) => f.to >= start && f.from <= end);
    if (seen.length === 0) return null;
    return { priceRange: { minValue: Math.min(...seen.map((f) => f.low)), maxValue: Math.max(...seen.map((f) => f.high)) } };
  }

  private draw(target: Target): void {
    const chart = this.chart;
    const series = this.series;
    if (!chart || !series) return;
    const ts = chart.timeScale();
    const x = (i: number) => ts.logicalToCoordinate(i as Logical);
    const y = (v: number) => series.priceToCoordinate(v);
    const hits: Hit[] = [];
    target.useMediaCoordinateSpace(({ context: c, mediaSize }) => {
      const bottom = mediaSize.height - 4;
      // Versions: a dashed line where a new model, prompt or schema starts.
      c.save();
      c.setLineDash([3, 4]);
      c.strokeStyle = "#5C6B7E";
      c.fillStyle = "#A3ADBB";
      c.font = "10px IBM Plex Mono, monospace";
      for (const i of this.data.versions) {
        const xi = x(i);
        if (xi === null) continue;
        c.beginPath();
        c.moveTo(xi, 0);
        c.lineTo(xi, bottom - RIBBON - 4);
        c.stroke();
        c.fillText("nouvelle version", xi + 4, 12);
      }
      c.restore();
      // Fans: from the price on the day of the advice to [p10, p90] at the deadline.
      for (const f of this.data.fans) {
        const x0 = x(f.from);
        const x1 = x(f.to);
        const y0 = y(f.base);
        const yl = y(f.low);
        const yh = y(f.high);
        if (x0 === null || x1 === null || y0 === null || yl === null || yh === null) continue;
        const on = this.selected.includes(f.seq);
        c.save();
        c.globalAlpha = on ? 0.38 : 0.16;
        c.fillStyle = FAN;
        c.beginPath();
        c.moveTo(x0, y0);
        c.lineTo(x1, yh);
        c.lineTo(x1, yl);
        c.closePath();
        c.fill();
        c.globalAlpha = on ? 1 : 0.5;
        c.strokeStyle = FAN;
        c.lineWidth = on ? 1.5 : 1;
        c.stroke();
        c.setLineDash([3, 3]);
        c.beginPath();
        c.moveTo(x0, y0);
        c.lineTo(x1, (yl + yh) / 2);
        c.stroke();
        c.restore();
        if (f.pending) {
          c.fillStyle = "#A3ADBB";
          c.font = "11px IBM Plex Sans, sans-serif";
          c.fillText("en cours", x1 - 24, Math.min(yh, yl) - 6);
        }
        hits.push({ seq: f.seq, x: x1, y: (yl + yh) / 2 });
      }
      // Diamonds: an advice issued (shape) in the color of its verdict.
      for (const d of this.data.diamonds) {
        const xi = x(d.index);
        const yi = y(d.value);
        if (xi === null || yi === null) continue;
        const r = this.selected.includes(d.seq) ? 8 : 6;
        c.beginPath();
        c.moveTo(xi, yi - r);
        c.lineTo(xi + r, yi);
        c.lineTo(xi, yi + r);
        c.lineTo(xi - r, yi);
        c.closePath();
        c.fillStyle = d.color;
        c.fill();
        c.lineWidth = this.selected.includes(d.seq) ? 2 : 1.5;
        c.strokeStyle = this.selected.includes(d.seq) ? "#E8EBF0" : "#0B0E13";
        c.stroke();
        hits.push({ seq: d.seq, x: xi, y: yi });
      }
      // Results: a check in a green disc, a cross in a red one.
      for (const r of this.data.results) {
        const xi = x(r.index);
        const yi = y(r.value);
        if (xi === null || yi === null) continue;
        c.beginPath();
        c.arc(xi, yi, 7, 0, 2 * Math.PI);
        c.fillStyle = r.good ? GOOD : BAD;
        c.fill();
        c.lineWidth = 1.5;
        c.strokeStyle = "#0B0E13";
        c.stroke();
        c.strokeStyle = "#0B0E13";
        c.lineWidth = 2;
        c.beginPath();
        if (r.good) {
          c.moveTo(xi - 3.5, yi);
          c.lineTo(xi - 1, yi + 3);
          c.lineTo(xi + 4, yi - 3);
        } else {
          c.moveTo(xi - 3, yi - 3);
          c.lineTo(xi + 3, yi + 3);
          c.moveTo(xi + 3, yi - 3);
          c.lineTo(xi - 3, yi + 3);
        }
        c.stroke();
        hits.push({ seq: r.seq, x: xi, y: yi });
      }
      // Rings: a rise nobody announced.
      for (const g of this.data.rings) {
        const xi = x(g.index);
        const yi = y(g.value);
        if (xi === null || yi === null) continue;
        c.beginPath();
        c.arc(xi, yi, 6, 0, 2 * Math.PI);
        c.lineWidth = 2;
        c.strokeStyle = BAD;
        c.stroke();
        hits.push({ seq: g.seq, x: xi, y: yi });
      }
      // Ribbon: the verdict in force, under the curve.
      for (const s of this.data.ribbon) {
        const x0 = x(s.from);
        const x1 = x(s.to);
        if (x0 === null || x1 === null) continue;
        c.fillStyle = s.color;
        c.fillRect(x0, bottom - RIBBON, Math.max(x1 - x0, 2), RIBBON);
        if (s.dashed) {
          c.strokeStyle = "#121720";
          c.setLineDash([2, 3]);
          c.strokeRect(x0, bottom - RIBBON, Math.max(x1 - x0, 2), RIBBON);
          c.setLineDash([]);
        }
      }
    });
    this.hits = hits;
  }
}

/** Real price with the dated advices of the ledger. A click on a mark picks its advice. */
export function AdviceChart(props: {
  series: SeriesPayload;
  advices: LedgerAdvice[];
  versionDays: string[];
  today: string;
  selected: number[];
  onPick: (seq: number) => void;
  height?: number;
}) {
  const box = useRef<HTMLDivElement | null>(null);
  const prim = useRef<AdvicePrimitive | null>(null);
  const pick = useRef(props.onPick);
  pick.current = props.onPick;
  const points: Point[] = props.series.points;
  const data = useMemo(() => marks(points, props.advices, props.versionDays, props.today), [points, props.advices, props.versionDays, props.today]);

  useEffect(() => {
    if (!box.current) return;
    const chart: IChartApi = createChart(box.current, {
      height: props.height ?? 320,
      autoSize: true,
      // No built-in logo: it injects an inline <style> (blocked by the CSP) and its link carries the page address.
      layout: { background: { color: "#121720" }, textColor: "#A3ADBB", fontFamily: "IBM Plex Mono, monospace", attributionLogo: false },
      grid: { vertLines: { color: "#1A212C" }, horzLines: { color: "#1A212C" } },
      rightPriceScale: { borderColor: "#263040", scaleMargins: { top: 0.12, bottom: 0.14 } },
      timeScale: { borderColor: "#263040", rightOffset: 22 },
    });
    const line = chart.addSeries(LineSeries, { color: "#6CB2FF", lineWidth: 2, priceLineVisible: false });
    line.setData(points.map((p) => ({ time: p.day as Time, value: p.value })));
    const primitive = new AdvicePrimitive();
    line.attachPrimitive(primitive);
    prim.current = primitive;
    const onClick = (param: MouseEventParams<Time>) => {
      if (!param.point) return;
      const { x, y } = param.point;
      let best: Hit | null = null;
      let bestD = 14 * 14;
      for (const h of primitive.hits) {
        const d = (h.x - x) ** 2 + (h.y - y) ** 2;
        if (d < bestD) {
          best = h;
          bestD = d;
        }
      }
      if (best) pick.current(best.seq);
    };
    chart.subscribeClick(onClick);
    chart.timeScale().fitContent();
    return () => {
      chart.unsubscribeClick(onClick);
      chart.remove();
      prim.current = null;
    };
  }, [points, props.height]);

  useEffect(() => {
    prim.current?.set(data, props.selected);
  }, [data, props.selected]);

  const credit = sourceCredit(props.series.source);
  return (
    <>
      <div className="chart" ref={box} />
      {credit && <p className="credit">{credit}</p>}
      <p className="credit">
        Graphique :{" "}
        <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">
          TradingView Lightweight Charts
        </a>
      </p>
    </>
  );
}
