import React, { useEffect, useState } from 'react';
import { fetchTrades } from '../api';

interface Trade {
  symbol: string;
  entry_price: number;
  exit_price: number;
  entry_time: string;
  exit_time: string;
  invest_amount: number;
  position_size: number;
  leverage: number;
  result: string;
  exit_reason: string;
  holding_hours: number;
  actual_pct: number;
  profit_pct: number;
  profit_amount: number;
  capital_after: number;
}

export const TradesView: React.FC = () => {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadTrades = async () => {
      try {
        const data = await fetchTrades(100);
        setTrades(data);
      } catch (error) {
        console.error("Failed to fetch trades:", error);
      } finally {
        setLoading(false);
      }
    };
    loadTrades();
  }, []);

  if (loading) {
    return (
      <div className="p-20 text-center font-black uppercase text-4xl animate-pulse">
        Loading Trade History...
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div className="brut-border brut-shadow bg-accent p-6 text-white">
        <h2 className="text-4xl font-black uppercase">Historical Trades</h2>
        <p className="font-mono opacity-80 mt-2">Showing last {trades.length} completed transactions</p>
      </div>

      <div className="brut-border brut-shadow bg-white overflow-x-auto">
        <table className="w-full text-left border-collapse">
          <thead>
            <tr className="bg-black text-white uppercase font-bold">
              <th className="p-4 border-r border-white/20">Symbol</th>
              <th className="p-4 border-r border-white/20">Entry/Exit Price</th>
              <th className="p-4 border-r border-white/20">Leverage</th>
              <th className="p-4 border-r border-white/20">Result</th>
              <th className="p-4 border-r border-white/20">PnL %</th>
              <th className="p-4 border-r border-white/20">PnL $</th>
              <th className="p-4">Time</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((trade, i) => (
              <tr key={i} className="border-b-4 border-black hover:bg-gray-50 transition-colors">
                <td className="p-4 font-black border-r-4 border-black">{trade.symbol}</td>
                <td className="p-4 border-r-4 border-black font-mono">
                   {trade.entry_price.toFixed(4)} &rarr; {trade.exit_price.toFixed(4)}
                </td>
                <td className="p-4 border-r-4 border-black">{trade.leverage}x</td>
                <td className="p-4 border-r-4 border-black">
                  <span className={`px-2 py-1 brut-border font-bold uppercase text-xs ${
                    ['success', 'tp_initial', 'tp_reduced', 'tp_after_add'].includes(trade.result) ? 'bg-green-400' : 'bg-red-400'
                  }`}>
                    {trade.result}
                  </span>
                  <div className="text-[10px] opacity-60 mt-1">{trade.exit_reason}</div>
                </td>
                <td className={`p-4 border-r-4 border-black font-black ${
                  trade.profit_pct >= 0 ? 'text-green-600' : 'text-red-600'
                }`}>
                  {trade.profit_pct >= 0 ? '+' : ''}{trade.profit_pct.toFixed(2)}%
                </td>
                <td className={`p-4 border-r-4 border-black font-black ${
                  trade.profit_amount >= 0 ? 'text-green-600' : 'text-red-600'
                }`}>
                  ${trade.profit_amount.toFixed(2)}
                </td>
                <td className="p-4 font-mono text-xs opacity-70">
                  {new Date(trade.entry_time).toLocaleString()} <br/>
                  Hold: {trade.holding_hours.toFixed(1)}h
                </td>
              </tr>
            ))}
            {trades.length === 0 && (
              <tr>
                <td colSpan={7} className="p-20 text-center font-bold opacity-40 uppercase">
                  No trades found in database
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
