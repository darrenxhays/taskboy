// hand-rolled svg charts following the dataviz mark specs: thin marks, 2px surface
// gaps between fills, rounded data ends, hairline grid, hover tooltips, legend for >= 2 series.

import { useState } from "react";
import { formatTokens } from "../api";

export type Segment = { key: string; color: string; value: number };
export type Bucket = { label: string; tooltipLabel: string; segments: Segment[] };

const CHART_INK = "var(--text-muted)";

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const multiplier of [1, 2, 2.5, 5, 10]) {
    if (value <= magnitude * multiplier) return magnitude * multiplier;
  }
  return magnitude * 10;
}

export function StackedBars({ buckets, seriesOrder, height = 180 }: { buckets: Bucket[]; seriesOrder: { key: string; color: string }[]; height?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const width = 720;
  const pad = { top: 8, right: 8, bottom: 22, left: 44 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const totals = buckets.map((bucket) => bucket.segments.reduce((sum, segment) => sum + segment.value, 0));
  const max = niceMax(Math.max(...totals, 1));
  const step = plotW / Math.max(buckets.length, 1);
  const barW = Math.max(Math.min(step * 0.6, 34), 3);
  const gridLines = [0.25, 0.5, 0.75, 1];
  const active = hover !== null ? buckets[hover] : null;

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="stacked bar chart" onMouseLeave={() => setHover(null)}>
        {gridLines.map((fraction) => (
          <g key={fraction}>
            <line x1={pad.left} x2={width - pad.right} y1={pad.top + plotH * (1 - fraction)} y2={pad.top + plotH * (1 - fraction)} stroke="var(--grid)" strokeWidth={1} />
            <text x={pad.left - 6} y={pad.top + plotH * (1 - fraction) + 3} textAnchor="end" fontSize={9} fill={CHART_INK} className="tnum">
              {formatTokens(max * fraction)}
            </text>
          </g>
        ))}
        <line x1={pad.left} x2={width - pad.right} y1={pad.top + plotH} y2={pad.top + plotH} stroke="var(--axis)" strokeWidth={1} />
        {buckets.map((bucket, i) => {
          const x = pad.left + step * i + (step - barW) / 2;
          let y = pad.top + plotH;
          const drawn = bucket.segments.filter((segment) => segment.value > 0);
          return (
            <g key={i} onMouseEnter={() => setHover(i)}>
              {/* hit target wider than the mark */}
              <rect x={pad.left + step * i} y={pad.top} width={step} height={plotH} fill="transparent" />
              {drawn.map((segment, j) => {
                const h = (segment.value / max) * plotH;
                y -= h;
                const isTop = j === drawn.length - 1;
                const r = Math.min(3, barW / 2, h);
                const drawH = Math.max(h - (isTop ? 0 : 2), 0.75); // 2px surface gap between stacked fills
                if (isTop && h > 1.5) {
                  return <path key={segment.key} d={`M ${x} ${y + drawH} L ${x} ${y + r} Q ${x} ${y} ${x + r} ${y} L ${x + barW - r} ${y} Q ${x + barW} ${y} ${x + barW} ${y + r} L ${x + barW} ${y + drawH} Z`} fill={segment.color} opacity={hover === null || hover === i ? 1 : 0.45} />;
                }
                return <rect key={segment.key} x={x} y={y} width={barW} height={drawH} fill={segment.color} opacity={hover === null || hover === i ? 1 : 0.45} />;
              })}
              {bucket.label && (
                <text x={pad.left + step * i + step / 2} y={height - 8} textAnchor="middle" fontSize={9} fill={CHART_INK}>
                  {bucket.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {active && hover !== null && (
        <div
          className="pointer-events-none absolute top-1 rounded-md border px-3 py-2 text-[11px] shadow-lg"
          style={{
            left: `${Math.min((hover + 0.5) / buckets.length, 0.82) * 100}%`,
            borderColor: "var(--hairline-strong)",
            background: "var(--surface-2)",
            zIndex: 10,
          }}
        >
          <div className="mb-1 font-semibold" style={{ color: "var(--text-primary)" }}>
            {active.tooltipLabel}
          </div>
          {active.segments
            .filter((segment) => segment.value > 0)
            .map((segment) => (
              <div key={segment.key} className="flex items-center gap-2">
                <span className="h-2 w-2 rounded-sm" style={{ background: segment.color }} />
                <span style={{ color: "var(--text-secondary)" }}>{segment.key}</span>
                <span className="tnum ml-auto pl-3" style={{ color: "var(--text-primary)" }}>
                  {formatTokens(segment.value)}
                </span>
              </div>
            ))}
          <div className="mt-1 border-t pt-1 text-right" style={{ borderColor: "var(--hairline)", color: "var(--text-secondary)" }}>
            Σ {formatTokens(totals[hover])}
          </div>
        </div>
      )}
      <Legend series={seriesOrder} />
    </div>
  );
}

export function Legend({ series }: { series: { key: string; color: string }[] }) {
  if (series.length < 2) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
      {series.map((item) => (
        <span key={item.key} className="inline-flex items-center gap-1.5 text-[11px]" style={{ color: "var(--text-secondary)" }}>
          <span className="h-2 w-2 rounded-sm" style={{ background: item.color }} />
          {item.key}
        </span>
      ))}
    </div>
  );
}

// horizontal per-series breakdown: one thin track, 2px gaps, labels in ink
export function BreakdownBars({ rows }: { rows: { label: string; color: string; value: number; detail?: string }[] }) {
  const max = Math.max(...rows.map((row) => row.value), 1);
  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <div key={row.label} className="grid grid-cols-[64px_1fr_auto] items-center gap-2">
          <span className="mono truncate text-[11px]" style={{ color: "var(--text-secondary)" }}>
            {row.label}
          </span>
          <div className="h-3 overflow-hidden rounded-[3px]" style={{ background: "var(--surface-2)" }}>
            <div className="h-full rounded-[3px]" style={{ width: `${Math.max((row.value / max) * 100, row.value > 0 ? 1.5 : 0)}%`, background: row.color }} title={row.detail} />
          </div>
          <span className="tnum text-[11px]" style={{ color: "var(--text-primary)" }}>
            {formatTokens(row.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

// capacity gauge (ui chrome, not a data series): running slots vs max concurrency
export function CapacityGauge({ value, max, label }: { value: number; max: number; label: string }) {
  const fraction = max > 0 ? Math.min(value / max, 1) : 0;
  const radius = 44;
  const circumference = Math.PI * radius; // half circle
  return (
    <div className="flex flex-col items-center">
      <svg viewBox="0 0 120 70" className="w-full max-w-[180px]">
        <path d={`M 16 62 A ${radius} ${radius} 0 0 1 104 62`} fill="none" stroke="var(--surface-2)" strokeWidth={9} strokeLinecap="round" />
        <path d={`M 16 62 A ${radius} ${radius} 0 0 1 104 62`} fill="none" stroke={fraction >= 1 ? "var(--status-warning)" : "var(--accent)"} strokeWidth={9} strokeLinecap="round" strokeDasharray={`${circumference * fraction} ${circumference}`} />
        <text x={60} y={52} textAnchor="middle" fontSize={22} fontWeight={700} fill="var(--text-primary)" className="tnum">
          {value}
          <tspan fontSize={11} fill="var(--text-muted)">
            /{max}
          </tspan>
        </text>
      </svg>
      <span className="panel-title -mt-1">{label}</span>
    </div>
  );
}
