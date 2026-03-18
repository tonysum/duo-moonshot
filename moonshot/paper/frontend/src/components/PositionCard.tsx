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
  lowest_price?: number | null;
  has_added_position?: boolean;
  surge_pct?: number;
}

export const PositionCard: React.FC<PositionProps> = (pos) => {
  const isProfit = pos.profit_pct >= 0;
  
  // Hold time calculation
  const entryDate = new Date(pos.entry_time);
  const now = new Date();
  const holdMs = now.getTime() - entryDate.getTime();
  const holdHours = Math.floor(holdMs / 3600000);
  const holdMins = Math.floor((holdMs % 3600000) / 60000);
  const holdStr = holdHours >= 24 
    ? `${Math.floor(holdHours / 24)}d ${holdHours % 24}h`
    : `${holdHours}h ${holdMins}m`;

  // Distance to TP/SL (short: TP is below entry, SL is above)
  const tpDistance = pos.tp_price > 0 
    ? ((pos.current_price - pos.tp_price) / pos.current_price * 100).toFixed(1)
    : null;
  const slDistance = pos.sl_price > 0
    ? ((pos.sl_price - pos.current_price) / pos.current_price * 100).toFixed(1)
    : null;

  // Progress bar: how far price has moved toward TP (0% = entry, 100% = TP)
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
          <h3 className="text-2xl font-black uppercase">
            {pos.symbol}
            {pos.has_added_position && (
              <span className="ml-2 text-xs bg-blue-200 dark:bg-blue-800 px-1.5 py-0.5 brut-border">2×</span>
            )}
          </h3>
          <div className="flex gap-3 text-xs font-mono text-muted-foreground mt-1">
            <span>⏱ {holdStr}</span>
            <span>{pos.leverage}× SHORT</span>
            {pos.surge_pct && <span className="text-emerald-600">+{pos.surge_pct.toFixed(0)}%</span>}
          </div>
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
          <p className="font-black">${pos.entry_price.toFixed(pos.entry_price > 100 ? 2 : 5)}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase font-bold text-muted-foreground">Current</p>
          <p className="font-black">${pos.current_price.toFixed(pos.current_price > 100 ? 2 : 5)}</p>
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

      {/* TP/SL Info */}
      <div className="flex gap-4 text-xs font-mono">
        {tpDistance && (
          <span className="bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-400 px-2 py-0.5 brut-border">
            TP {tpDistance}% away
          </span>
        )}
        {slDistance && (
          <span className="bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-400 px-2 py-0.5 brut-border">
            SL {slDistance}% away
          </span>
        )}
        {pos.lowest_price && (
          <span className="bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-400 px-2 py-0.5 brut-border">
            Low: ${pos.lowest_price.toFixed(pos.lowest_price > 100 ? 2 : 5)}
          </span>
        )}
      </div>
    </div>
  );
};
