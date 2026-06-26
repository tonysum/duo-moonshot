import { useEffect, useRef, useState } from 'react';
import {
  createChart,
  ColorType,
  AreaSeries,
  type IChartApi,
  type ISeriesApi,
  type AreaData,
  type UTCTimestamp,
} from 'lightweight-charts';
import { fetchEquity, fetchSummary, fetchTrades } from '../api';

export type EquityPoint = {
  timestamp: string;
  total_equity: number;
  cash?: number;
};

interface EquityCurveViewProps {
  strategy?: 'daily' | 'rolling';
}

function toChartTime(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso.replace('Z', '+00:00')).getTime() / 1000) as UTCTimestamp;
}

function dedupeByTime(points: EquityPoint[]): EquityPoint[] {
  const map = new Map<number, EquityPoint>();
  for (const p of points) {
    map.set(toChartTime(p.timestamp), p);
  }
  return [...map.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([, p]) => p);
}

function computeMaxDrawdown(values: number[]): number {
  let peak = -Infinity;
  let maxDd = 0;
  for (const v of values) {
    if (v > peak) peak = v;
    if (peak > 0) {
      const dd = ((peak - v) / peak) * 100;
      if (dd > maxDd) maxDd = dd;
    }
  }
  return maxDd;
}

async function loadEquityData(strategy?: 'daily' | 'rolling'): Promise<{
  points: EquityPoint[];
  initialCapital: number;
  currentEquity: number;
  source: 'equity' | 'summary' | 'trades';
}> {
  const equity = await fetchEquity();
  if (equity.length > 0) {
    const summary = await fetchSummary(strategy);
    return {
      points: equity,
      initialCapital: summary.initial_capital ?? equity[0].total_equity,
      currentEquity: equity[equity.length - 1].total_equity,
      source: 'equity',
    };
  }

  const summary = await fetchSummary(strategy);
  const curve = summary.equity_curve as EquityPoint[] | undefined;
  if (curve && curve.length > 0) {
    return {
      points: curve,
      initialCapital: summary.initial_capital ?? curve[0].total_equity,
      currentEquity: curve[curve.length - 1].total_equity,
      source: 'summary',
    };
  }

  const trades = await fetchTrades(9999, strategy);
  if (Array.isArray(trades) && trades.length > 0) {
    const sorted = [...trades].reverse();
    const initial = summary.initial_capital ?? sorted[0].capital_after ?? 10000;
    const tradePoints: EquityPoint[] = sorted
      .filter((t) => t.exit_time && t.capital_after != null)
      .map((t) => ({
        timestamp: t.exit_time,
        total_equity: t.capital_after,
        cash: t.capital_after,
      }));
    const points =
      tradePoints.length > 0
        ? [{ timestamp: sorted[0].entry_time ?? tradePoints[0].timestamp, total_equity: initial }, ...tradePoints]
        : [];
    return {
      points,
      initialCapital: initial,
      currentEquity: points.length ? points[points.length - 1].total_equity : initial,
      source: 'trades',
    };
  }

  return {
    points: [],
    initialCapital: summary.initial_capital ?? 10000,
    currentEquity: summary.current_capital ?? summary.initial_capital ?? 10000,
    source: 'equity',
  };
}

export function EquityCurveView({ strategy }: EquityCurveViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null);

  const [loading, setLoading] = useState(true);
  const [points, setPoints] = useState<EquityPoint[]>([]);
  const [initialCapital, setInitialCapital] = useState(0);
  const [source, setSource] = useState<'equity' | 'summary' | 'trades'>('equity');
  const [showCash, setShowCash] = useState(false);

  useEffect(() => {
    setLoading(true);
    loadEquityData(strategy)
      .then((d) => {
        setPoints(dedupeByTime(d.points));
        setInitialCapital(d.initialCapital);
        setSource(d.source);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
    const timer = setInterval(() => {
      loadEquityData(strategy).then((d) => {
        setPoints(dedupeByTime(d.points));
        setInitialCapital(d.initialCapital);
        setSource(d.source);
      }).catch(() => {});
    }, 60000);
    return () => clearInterval(timer);
  }, [strategy]);

  useEffect(() => {
    if (!containerRef.current || points.length === 0) return;

    const isDark = document.documentElement.classList.contains('dark');
    const bg = isDark ? '#111827' : '#ffffff';
    const text = isDark ? '#9ca3af' : '#6b7280';
    const grid = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';

    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
      seriesRef.current = null;
    }

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 420,
      layout: { background: { type: ColorType.Solid, color: bg }, textColor: text },
      grid: { vertLines: { color: grid }, horzLines: { color: grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
    });

    const series = chart.addSeries(AreaSeries, {
      lineColor: '#ea580c',
      topColor: 'rgba(234, 88, 12, 0.35)',
      bottomColor: 'rgba(234, 88, 12, 0.02)',
      lineWidth: 2,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });

    const chartData: AreaData<UTCTimestamp>[] = points.map((p) => ({
      time: toChartTime(p.timestamp),
      value: showCash ? (p.cash ?? p.total_equity) : p.total_equity,
    }));

    series.setData(chartData);

    if (initialCapital > 0) {
      series.createPriceLine({
        price: initialCapital,
        color: isDark ? '#6b7280' : '#9ca3af',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: 'Initial',
      });
    }

    chart.timeScale().fitContent();
    chartRef.current = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver(() => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [points, initialCapital, showCash]);

  if (loading) {
    return <div className="brut-border p-12 text-center font-black text-2xl animate-blink">Loading Equity Curve...</div>;
  }

  if (points.length === 0) {
    return (
      <div className="brut-border border-dashed p-12 text-center text-muted-foreground font-bold uppercase">
        No equity data yet. Snapshots are recorded every 5 minutes while the runner is active.
      </div>
    );
  }

  const values = points.map((p) => (showCash ? (p.cash ?? p.total_equity) : p.total_equity));
  const latest = points[points.length - 1];
  const displayValue = showCash ? (latest.cash ?? latest.total_equity) : latest.total_equity;
  const returnPct = initialCapital > 0 ? ((displayValue / initialCapital - 1) * 100) : 0;
  const maxDd = computeMaxDrawdown(values);
  const pnlColor = returnPct >= 0 ? 'text-emerald-600' : 'text-red-600';
  const pnlBg = returnPct >= 0 ? 'bg-emerald-50 dark:bg-emerald-950/30' : 'bg-red-50 dark:bg-red-950/30';

  const sourceLabel =
    source === 'equity' ? '5-min snapshots' : source === 'summary' ? 'summary cache' : 'trade closes';

  return (
    <div className="space-y-6">
      <div className={`brut-border brut-shadow p-6 ${pnlBg}`}>
        <div className="flex flex-wrap justify-between items-center gap-4 mb-4">
          <h2 className="text-2xl font-black uppercase">📈 Equity Curve</h2>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setShowCash(false)}
              className={`brut-border px-3 py-1 text-sm font-bold ${!showCash ? 'bg-accent text-white' : 'bg-white dark:bg-gray-800'}`}
            >
              Total Equity
            </button>
            <button
              type="button"
              onClick={() => setShowCash(true)}
              className={`brut-border px-3 py-1 text-sm font-bold ${showCash ? 'bg-accent text-white' : 'bg-white dark:bg-gray-800'}`}
            >
              Cash Only
            </button>
          </div>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <Stat label="Initial Capital" value={`$${initialCapital.toLocaleString(undefined, { minimumFractionDigits: 2 })}`} />
          <Stat
            label={showCash ? 'Current Cash' : 'Current Equity'}
            value={`$${displayValue.toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
          />
          <Stat
            label="Return"
            value={`${returnPct >= 0 ? '+' : ''}${returnPct.toFixed(2)}%`}
            className={pnlColor}
          />
          <Stat label="Max Drawdown" value={`${maxDd.toFixed(2)}%`} className="text-red-600" />
        </div>
        <p className="text-[10px] text-muted-foreground mt-3 font-mono">
          {points.length} data points · source: {sourceLabel}
          {!showCash && ' · includes unrealized PnL'}
        </p>
      </div>

      <div className="brut-border brut-shadow bg-white dark:bg-gray-900 p-4">
        <div ref={containerRef} className="w-full" />
      </div>
    </div>
  );
}

function Stat({ label, value, className = '' }: { label: string; value: string; className?: string }) {
  return (
    <div className="p-3 brut-border bg-white dark:bg-gray-800">
      <p className="text-[10px] font-bold text-muted-foreground uppercase">{label}</p>
      <p className={`text-2xl font-black ${className}`}>{value}</p>
    </div>
  );
}
