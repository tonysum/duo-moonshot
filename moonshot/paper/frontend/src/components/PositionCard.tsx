import React from 'react';

interface PositionProps {
  symbol: string;
  entry_price: number;
  current_price: number;
  invest_amount: number;
  profit_pct: number;
  unrealized_pnl: number;
  entry_time: string;
  leverage: number;
  tp_price: number;
  sl_price: number;
  target_pct?: number;
  stop_loss_pct?: number;
  entry_reason?: string;
  add_price?: number | null;
  add_time?: string | null;
  lowest_price?: number | null;
  highest_price?: number | null;
  has_added_position?: boolean;
  surge_pct?: number;
  strategy?: string;
}

function fmtPrice(v: number): string {
  return v > 100 ? v.toFixed(2) : v.toFixed(5);
}

function fmtEntryTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  } catch { return iso.slice(0, 16); }
}

export const PositionCard: React.FC<PositionProps> = (pos) => {
  const isProfit = pos.profit_pct >= 0;

  const entryDate = new Date(pos.entry_time);
  const now = new Date();
  const holdMs = now.getTime() - entryDate.getTime();
  const holdHours = Math.floor(holdMs / 3600000);
  const holdMins = Math.floor((holdMs % 3600000) / 60000);
  const holdStr = holdHours >= 24
    ? `${Math.floor(holdHours / 24)}d ${holdHours % 24}h`
    : `${holdHours}h ${holdMins}m`;

  const tpDistance = pos.tp_price > 0
    ? ((pos.current_price - pos.tp_price) / pos.current_price * 100).toFixed(1)
    : null;
  const slDistance = pos.sl_price > 0
    ? ((pos.sl_price - pos.current_price) / pos.current_price * 100).toFixed(1)
    : null;

  const highWater =
    pos.highest_price != null && pos.highest_price > 0
      ? Math.max(pos.highest_price, pos.current_price)
      : Math.max(pos.entry_price, pos.current_price);
  const slRoomFromHigh =
    pos.sl_price > 0 && highWater > 0
      ? ((pos.sl_price - highWater) / highWater * 100).toFixed(2)
      : null;

  const range = pos.entry_price - pos.tp_price;
  const moved = pos.entry_price - pos.current_price;
  const progress = range !== 0 ? Math.min(100, Math.max(-50, (moved / range) * 100)) : 0;

  return (
    <div className={`brut-border brut-shadow bg-white dark:bg-gray-900 p-5 flex flex-col gap-3 relative overflow-hidden ${
      isProfit ? 'border-l-green-500' : 'border-l-red-500'
    }`} style={{ borderLeftWidth: 6 }}>
      {/* Header */}
      <div className="flex justify-between items-start">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-2xl font-black uppercase">
              {pos.symbol}
              {pos.has_added_position && (
                <span className="ml-2 text-xs bg-blue-200 dark:bg-blue-800 px-1.5 py-0.5 brut-border">2×</span>
              )}
            </h3>
            {pos.strategy && (
              <span className={`text-[10px] font-bold px-2 py-0.5 brut-border ${pos.strategy === 'daily' ? 'bg-amber-100 dark:bg-amber-900/50' : 'bg-cyan-100 dark:bg-cyan-900/50'}`}>
                {pos.strategy}
              </span>
            )}
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs font-mono text-muted-foreground mt-1">
            <span title="Entry time">{fmtEntryTime(pos.entry_time)}</span>
            <span>·</span>
            <span>⏱ {holdStr}</span>
            <span>·</span>
            <span>{pos.leverage}× SHORT</span>
            {pos.surge_pct != null && <span className="text-emerald-600">+{pos.surge_pct.toFixed(0)}%</span>}
          </div>
          {pos.entry_reason && (
            <p className="text-[10px] text-muted-foreground mt-0.5">{pos.entry_reason}</p>
          )}
        </div>
        <div className={`px-3 py-1 brut-border font-black text-lg ${isProfit ? 'bg-green-400' : 'bg-red-400'}`}>
          {isProfit ? '+' : ''}{pos.profit_pct.toFixed(2)}%
        </div>
      </div>

      {/* Progress bar */}
      <div className="w-full h-2 bg-gray-200 dark:bg-gray-700 brut-border overflow-hidden">
        <div
          className={`h-full transition-all duration-500 ${progress >= 0 ? 'bg-emerald-500' : 'bg-red-500'}`}
          style={{ width: `${Math.abs(Math.min(progress, 100))}%` }}
        />
      </div>

      {/* Price Grid */}
      <div className="grid grid-cols-2 gap-3 font-mono text-sm">
        <div>
          <p className="text-[10px] uppercase font-bold text-muted-foreground">Entry</p>
          <p className="font-black">${fmtPrice(pos.entry_price)}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase font-bold text-muted-foreground">Current</p>
          <p className="font-black">${fmtPrice(pos.current_price)}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase font-bold text-muted-foreground">TP / SL (price)</p>
          <p className="font-black text-xs">
            TP ${fmtPrice(pos.tp_price)} / SL ${fmtPrice(pos.sl_price)}
          </p>
        </div>
        <div>
          <p className="text-[10px] uppercase font-bold text-muted-foreground">TP / SL (%)</p>
          <p className="font-black text-xs">
            {pos.target_pct != null ? pos.target_pct : '—'}% / {pos.stop_loss_pct != null ? pos.stop_loss_pct : '—'}%
          </p>
        </div>
        <div>
          <p className="text-[10px] uppercase font-bold text-muted-foreground">Unrealized PnL</p>
          <p className={`font-black ${isProfit ? 'text-green-600' : 'text-red-600'}`}>
            {isProfit ? '+' : ''}${pos.unrealized_pnl.toFixed(2)}
          </p>
        </div>
        <div>
          <p className="text-[10px] uppercase font-bold text-muted-foreground">Invested</p>
          <p className="font-black">${pos.invest_amount.toFixed(2)}</p>
        </div>
      </div>

      {/* TP/SL distance + Add info */}
      <div className="flex flex-col gap-2 text-xs font-mono">
        <div className="flex flex-wrap gap-2">
          {tpDistance != null && (
            <span className="bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-400 px-2 py-0.5 brut-border">
              TP {tpDistance}% away
            </span>
          )}
          {slDistance != null && (
            <span className="bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-400 px-2 py-0.5 brut-border">
              SL {slDistance}% away
            </span>
          )}
          {pos.has_added_position && pos.add_price != null && (
            <span className="bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400 px-2 py-0.5 brut-border">
              Add @ ${fmtPrice(pos.add_price)}
              {pos.add_time && ` (${fmtEntryTime(pos.add_time)})`}
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-2 items-center">
          {pos.lowest_price != null && (
            <span className="bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-400 px-2 py-0.5 brut-border">
              Low: ${fmtPrice(pos.lowest_price)}
            </span>
          )}
          <span
            className="bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-400 px-2 py-0.5 brut-border"
            title="空仓：持仓以来最高价 vs 止损价余量（%）；≤0 表示峰值已触及或超过止损价"
          >
            High: ${fmtPrice(highWater)}
            {slRoomFromHigh != null && (
              <span className={Number(slRoomFromHigh) <= 0 ? ' font-bold' : ''}>
                {' '}
                · vs SL: {Number(slRoomFromHigh) > 0 ? '+' : ''}
                {slRoomFromHigh}%
              </span>
            )}
          </span>
        </div>
      </div>
    </div>
  );
};
