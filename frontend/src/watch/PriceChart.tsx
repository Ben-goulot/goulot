import { createChart, createSeriesMarkers, LineSeries, type SeriesMarker, type Time } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { clip } from "../chain/types";
import { dayMarks, type EventMark, type Point } from "./chartView";
import { sourceCredit } from "./types";

const RETAINED = "#F28C4C";
const OTHER = "#7C8797";

function dayLabel(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
}

/** Price or index line for one period, with one marker per day where Goulot saw events. Colors from design/README.md. */
export function PriceChart(props: { points: Point[]; source?: string; events?: EventMark[]; height?: number }) {
  const box = useRef<HTMLDivElement | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const marks = dayMarks(props.events ?? [], props.points);
  const signature = JSON.stringify(marks.map((m) => [m.day, m.text, m.retained]));

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
      // No empty space on either side: the period fills the width.
      timeScale: { borderColor: "#263040", fixLeftEdge: true, fixRightEdge: true },
    });
    const line = chart.addSeries(LineSeries, { color: "#6CB2FF", lineWidth: 2 });
    line.setData(props.points.map((p) => ({ time: p.day as Time, value: p.value })));
    const markers: SeriesMarker<Time>[] = marks.map((m) => ({
      time: m.day as Time,
      position: "aboveBar",
      color: m.retained ? RETAINED : OTHER,
      shape: "circle",
      text: m.text,
    }));
    createSeriesMarkers(line, markers);
    chart.subscribeCrosshairMove((param) => setHovered(typeof param.time === "string" ? param.time : null));
    // The chart measures its box after creation: fitted before that, the line would stay squeezed in a corner.
    const fit = () => chart.timeScale().fitContent();
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(box.current);
    return () => {
      observer.disconnect();
      chart.remove();
    };
    // `signature` stands for `marks`, rebuilt at each render.
  }, [props.points, signature, props.height]);

  const credit = sourceCredit(props.source);
  const here = marks.find((m) => m.day === hovered);
  return (
    <>
      <div className="chart" ref={box} />
      {marks.length > 0 && (
        <p className="chart-note">
          {here ? (
            <>
              <strong>{dayLabel(here.day)}</strong> :{" "}
              {here.events.map((e) => `${e.letter}. ${clip(e.title, 90)}`).join(" · ")}
            </>
          ) : (
            "Chaque repère marque le jour où Goulot a vu des événements : orange s'ils justifient le verdict, gris sinon, avec leur nombre. Survole-le pour le détail ; les lettres renvoient à la liste « Événements »."
          )}
        </p>
      )}
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
