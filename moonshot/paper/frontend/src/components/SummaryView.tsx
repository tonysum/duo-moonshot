import { useState, useEffect } from 'react';
import { downloadPaperTradesCsv, fetchSummary } from '../api';

interface SummaryViewProps {
  strategy?: 'daily' | 'rolling';
}

export function SummaryView({ strategy }: SummaryViewProps) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    fetchSummary(strategy)
      .then(setData)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [strategy]);

  if (loading) return <div className="brut-border p-12 text-center font-black text-2xl animate-blink">Loading Summary...</div>;
  if (!data || data.total_trades === 0) {
    return (
      <div className="brut-border border-dashed p-12 text-center text-muted-foreground font-bold uppercase">
        No completed trades yet. Summary will appear after first trade closes.
      </div>
    );
  }

  const pnlColor = data.total_pnl >= 0 ? 'text-emerald-600' : 'text-red-600';
  const pnlBg = data.total_pnl >= 0 ? 'bg-emerald-50 dark:bg-emerald-950/30' : 'bg-red-50 dark:bg-red-950/30';
  const symbols = Object.entries(data.symbols || {}) as [string, any][];
  symbols.sort((a, b) => b[1].total_pnl - a[1].total_pnl);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className={`brut-border brut-shadow p-6 ${pnlBg}`}>
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-2xl font-black uppercase">📊 Performance Summary</h2>
          <button
            type="button"
            disabled={exporting}
            onClick={async () => {
              setExporting(true);
              try {
                await downloadPaperTradesCsv(strategy, true);
              } catch (e) {
                console.error(e);
                alert(e instanceof Error ? e.message : 'Export failed');
              } finally {
                setExporting(false);
              }
            }}
            className="brut-border brut-shadow-hover px-4 py-2 bg-white dark:bg-gray-800 font-bold text-sm uppercase hover:bg-gray-100 disabled:opacity-50"
            title="列与回测 moonshot / rolling 的 export_csv 一致，UTF-8 BOM，含表尾 Summary"
          >
            {exporting ? '…' : '📥 Export trades CSV'}
          </button>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <Stat label="Net PnL" value={`$${data.total_pnl.toLocaleString(undefined, {minimumFractionDigits:2})}`} className={pnlColor} />
          <Stat label="Return" value={`${data.total_return_pct >= 0 ? '+' : ''}${data.total_return_pct}%`} className={pnlColor} />
          <Stat label="Capital" value={`$${data.current_capital.toLocaleString(undefined, {minimumFractionDigits:2})}`} />
          <Stat label="Win Rate" value={`${data.win_rate}%`} className={data.win_rate >= 50 ? 'text-emerald-600' : 'text-red-600'} />
        </div>
      </div>

      {/* Metrics Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
        <div className="brut-border brut-shadow bg-white dark:bg-gray-900 p-6">
          <h3 className="text-lg font-black uppercase mb-4">Trade Statistics</h3>
          <div className="space-y-3 font-mono text-sm">
            <Row label="Total Trades" value={data.total_trades} />
            <Row label="Wins / Losses" value={`${data.winning_trades}W / ${data.losing_trades}L`} />
            <Row label="Profit Factor" value={data.profit_factor >= 99 ? '∞' : data.profit_factor} />
            <Row label="Max Consec Loss" value={data.max_consecutive_losses} />
            <Row label="Add Positions" value={data.added_positions} />
          </div>
        </div>

        <div className="brut-border brut-shadow bg-white dark:bg-gray-900 p-6">
          <h3 className="text-lg font-black uppercase mb-4">Holding Time</h3>
          <div className="space-y-3 font-mono text-sm">
            <Row label="Avg Hold" value={formatHours(data.avg_hold_hours)} />
            <Row label="Avg Win Hold" value={formatHours(data.avg_win_hold)} />
            <Row label="Avg Loss Hold" value={formatHours(data.avg_loss_hold)} />
          </div>
        </div>
      </div>

      {/* Per-Symbol Table */}
      {symbols.length > 0 && (
        <div className="brut-border brut-shadow bg-white dark:bg-gray-900 p-6">
          <h3 className="text-lg font-black uppercase mb-4">Per-Symbol Breakdown</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="border-b-4 border-black dark:border-gray-600 text-left">
                  <th className="py-2 font-black">Symbol</th>
                  <th className="py-2 font-black text-center">Trades</th>
                  <th className="py-2 font-black text-center">Win Rate</th>
                  <th className="py-2 font-black text-right">PnL</th>
                </tr>
              </thead>
              <tbody>
                {symbols.map(([sym, st]) => (
                  <tr key={sym} className="border-b border-gray-200 dark:border-gray-700 hover:bg-slate-50 dark:hover:bg-gray-800">
                    <td className="py-2 font-bold">{sym}</td>
                    <td className="py-2 text-center">{st.trades}</td>
                    <td className="py-2 text-center">
                      <span className={st.win_rate >= 50 ? 'text-emerald-600' : 'text-red-600'}>
                        {st.win_rate}%
                      </span>
                    </td>
                    <td className={`py-2 text-right font-bold ${st.total_pnl >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>
                      {st.total_pnl >= 0 ? '+' : ''}{st.total_pnl.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, className = '' }: { label: string; value: string | number; className?: string }) {
  return (
    <div className="p-3 brut-border bg-white dark:bg-gray-800">
      <p className="text-[10px] font-bold text-muted-foreground uppercase">{label}</p>
      <p className={`text-2xl font-black ${className}`}>{value}</p>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex justify-between items-center">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-bold">{value}</span>
    </div>
  );
}

function formatHours(h: number): string {
  if (!h) return '0h';
  if (h >= 24) return `${Math.floor(h / 24)}d ${Math.round(h % 24)}h`;
  return `${h.toFixed(1)}h`;
}
