import { createChart, createSeriesMarkers, LineSeries, type IChartApi, type SeriesMarker, type Time } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { sourceCredit, type SeriesPayload } from "./types";

export type ChartMark = { day: string; text: string; color: string; shape: "circle" | "arrowUp" | "arrowDown" | "square" };

/** Price or index line with dated markers (events, advices). Colors come from design/README.md. */
export function PriceChart(props: { series: SeriesPayload; marks?: ChartMark[]; height?: number }) {
  const box = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, {
      height: props.height ?? 280,
      autoSize: true,
      // No built-in logo: it injects an inline <style> (blocked by the CSP) and its link carries the page address,
      // which holds a material id. The attribution is written below the chart instead.
      layout: { background: { color: "#121720" }, textColor: "#A3ADBB", fontFamily: "IBM Plex Mono, monospace", attributionLogo: false },
      grid: { vertLines: { color: "#1A212C" }, horzLines: { color: "#1A212C" } },
      rightPriceScale: { borderColor: "#263040" },
      timeScale: { borderColor: "#263040" },
    });
    chartRef.current = chart;
    const line = chart.addSeries(LineSeries, { color: "#6CB2FF", lineWidth: 2 });
    line.setData(props.series.points.map((p) => ({ time: p.day as Time, value: p.value })));
    const days = new Set(props.series.points.map((p) => p.day));
    const first = props.series.points[0]?.day ?? "";
    const marks: SeriesMarker<Time>[] = (props.marks ?? [])
      .filter((m) => m.day >= first)
      .map((m) => {
        // A marker must sit on a day of the series: take the next available day.
        const day = days.has(m.day) ? m.day : props.series.points.find((p) => p.day >= m.day)?.day ?? m.day;
        const marker: SeriesMarker<Time> = { time: day as Time, position: "aboveBar", color: m.color, shape: m.shape, text: m.text };
        return marker;
      })
      .sort((a, b) => String(a.time).localeCompare(String(b.time)));
    createSeriesMarkers(line, marks);
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [props.series, props.marks, props.height]);

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
