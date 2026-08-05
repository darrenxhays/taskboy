import { api, formatTokens, formatUsd, modelColor, modelLabel, type UsageCard } from "../api";
import { BreakdownBars, StackedBars, type Bucket } from "../components/charts";
import { EmptyState, ErrorNote, Panel } from "../components/ui";
import { useLiveData } from "../stream";

// fixed categorical order — validated adjacency (cyan, orange, violet, green)
const SERIES_ORDER = ["sonnet", "opus", "fable", "haiku"];

function formatCountdown(resetsAt: number): string {
  const minutes = Math.ceil((resetsAt * 1000 - Date.now()) / 60_000);
  if (minutes <= 0) return "resets soon";
  if (minutes < 60) return `resets in ${minutes}m`;
  return `resets in ${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function Card({ card }: { card: UsageCard }) {
  const remaining = card.observed
    ? Math.max(0, Math.min(100, (1 - card.observed.utilization) * 100))
    : card.limit_tokens
      ? Math.max(0, Math.min(100, ((card.limit_tokens - card.total_tokens) / card.limit_tokens) * 100))
      : null;
  const meterColor = remaining === null ? undefined : remaining < 10 ? "var(--status-critical)" : remaining <= 25 ? "var(--status-warning)" : "var(--status-good)";
  const meterWidth = !card.observed && card.limit_tokens && card.total_tokens > card.limit_tokens ? 100 : remaining;
  return (
    <Panel title={card.label}>
      <div className="flex items-baseline gap-4">
        <div>
          <div className="tnum text-[30px] font-bold leading-none">{formatTokens(card.total_tokens)}</div>
          <div className="mt-1 text-[10px] uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
            total tokens
          </div>
        </div>
        <div className="ml-auto text-right">
          <div className="tnum text-[16px] font-semibold">{formatUsd(card.totals.cost_usd)}</div>
          <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
            {card.totals.task_count} tasks
          </div>
        </div>
      </div>
      {remaining !== null && (
        <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--hairline)" }}>
          <div className="mb-1.5 flex items-center justify-between text-[10px] uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
            <span>
              limit remaining
              {card.observed && <span className="ml-2">LIVE</span>}
            </span>
            <span className="tnum font-semibold" style={{ color: meterColor }}>
              {Math.round(remaining)}% remaining{card.observed && ` · ${formatCountdown(card.observed.resets_at)}`}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full" style={{ background: "var(--hairline)" }}>
            <div className="h-full rounded-full transition-[width]" style={{ width: `${meterWidth}%`, background: meterColor }} />
          </div>
        </div>
      )}
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 border-t pt-3 text-[11px]" style={{ borderColor: "var(--hairline)", color: "var(--text-secondary)" }}>
        <span>
          input <span className="tnum float-right">{formatTokens(card.totals.input_tokens)}</span>
        </span>
        <span>
          output <span className="tnum float-right">{formatTokens(card.totals.output_tokens)}</span>
        </span>
        <span>
          cache read <span className="tnum float-right">{formatTokens(card.totals.cache_read_tokens)}</span>
        </span>
        <span>
          cache write <span className="tnum float-right">{formatTokens(card.totals.cache_write_tokens)}</span>
        </span>
      </div>
      {card.by_model.length > 0 && (
        <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--hairline)" }}>
          <BreakdownBars
            rows={card.by_model.map((row) => ({
              label: modelLabel(row.model),
              color: modelColor(row.model),
              value: row.input_tokens + row.output_tokens + row.cache_tokens,
              detail: `${modelLabel(row.model)}: ${formatUsd(row.cost_usd)}`,
            }))}
          />
        </div>
      )}
    </Panel>
  );
}

function buildBuckets(timeseries: { bucket: string; model: string; total_tokens: number }[], mode: "hourly" | "daily"): { buckets: Bucket[]; series: { key: string; color: string }[] } {
  const now = new Date();
  const keys: string[] = [];
  const labels: string[] = [];
  const tooltips: string[] = [];
  if (mode === "hourly") {
    for (let i = 23; i >= 0; i--) {
      const moment = new Date(now.getTime() - i * 3600_000);
      const iso = moment.toISOString();
      keys.push(iso.slice(0, 13));
      const hour = iso.slice(11, 13);
      labels.push(i % 4 === 0 ? `${hour}:00` : "");
      tooltips.push(`${iso.slice(5, 10)} ${hour}:00 UTC`);
    }
  } else {
    for (let i = 6; i >= 0; i--) {
      const moment = new Date(now.getTime() - i * 86400_000);
      const iso = moment.toISOString();
      keys.push(iso.slice(0, 10));
      labels.push(iso.slice(5, 10));
      tooltips.push(iso.slice(0, 10));
    }
  }
  const present = new Set<string>();
  const sums = new Map<string, Map<string, number>>();
  for (const row of timeseries) {
    const key = mode === "hourly" ? row.bucket : row.bucket.slice(0, 10);
    const model = modelLabel(row.model);
    present.add(model);
    if (!sums.has(key)) sums.set(key, new Map());
    const inner = sums.get(key)!;
    inner.set(model, (inner.get(model) ?? 0) + row.total_tokens);
  }
  const order = [...SERIES_ORDER.filter((model) => present.has(model)), ...[...present].filter((model) => !SERIES_ORDER.includes(model)).sort()];
  const buckets = keys.map((key, i) => ({
    label: labels[i],
    tooltipLabel: tooltips[i],
    segments: order.map((model) => ({ key: model, color: modelColor(model), value: sums.get(key)?.get(model) ?? 0 })),
  }));
  return { buckets, series: order.map((model) => ({ key: model, color: modelColor(model) })) };
}

export function UsagePage() {
  const { data, error } = useLiveData(() => api.usage());
  if (error) return <ErrorNote message={error} />;
  if (!data) return <EmptyState label="loading usage…" />;
  const hourly = buildBuckets(data.timeseries, "hourly");
  const daily = buildBuckets(data.timeseries, "daily");

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-[18px] font-bold tracking-wide">USAGE TELEMETRY</h1>
        <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>
          tokens are primary — cost may read $0 under subscription auth
        </span>
      </header>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card card={data.cards.five_hour} />
        <Card card={data.cards.weekly} />
        <Card card={data.cards.fable} />
      </div>
      <Panel title="Tokens by hour — last 24h">{hourly.buckets.some((bucket) => bucket.segments.some((segment) => segment.value > 0)) ? <StackedBars buckets={hourly.buckets} seriesOrder={hourly.series} /> : <EmptyState label="no usage in the last 24 hours" />}</Panel>
      <Panel title="Tokens by day — last 7d">{daily.buckets.some((bucket) => bucket.segments.some((segment) => segment.value > 0)) ? <StackedBars buckets={daily.buckets} seriesOrder={daily.series} /> : <EmptyState label="no usage in the last 7 days" />}</Panel>
    </div>
  );
}
