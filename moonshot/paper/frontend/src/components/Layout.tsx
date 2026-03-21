import React from 'react';

export interface LayoutProps {
  children: React.ReactNode;
  activeView: 'dashboard' | 'trades' | 'summary';
  onViewChange: (view: 'dashboard' | 'trades' | 'summary') => void;
  prices: Record<string, number>;
  wsConnected: boolean;
  darkMode: boolean;
  onToggleDark: () => void;
  gainers?: { symbol: string; pct_chg: number; price: string; volume: number }[];
  scanResult?: { scan_time: string; gainers: { symbol: string; pct_chg: number; status: string; detail: string }[] } | null;
}

const TICKER_SYMBOLS = [
  { key: 'BTCUSDT', label: 'BTC', icon: '₿' },
  { key: 'ETHUSDT', label: 'ETH', icon: 'Ξ' },
  { key: 'BNBUSDT', label: 'BNB', icon: '◆' },
  { key: 'SOLUSDT', label: 'SOL', icon: '◎' },
];

export const Layout: React.FC<LayoutProps> = ({
  children, activeView, onViewChange, prices, wsConnected, darkMode, onToggleDark,
  gainers = [], scanResult = null
}) => {
  const tickerContent = TICKER_SYMBOLS.map(({ key, label, icon }) => {
    const price = prices[key];
    return `${icon} ${label}: $${price ? price.toLocaleString(undefined, { minimumFractionDigits: 2 }) : '---'}`;
  }).join('     ·     ');

  return (
    <div className="min-h-screen dot-grid-bg dark:dark-grid-bg text-foreground dark:text-gray-100 selection:bg-accent selection:text-white flex flex-col">
      {/* Price Ticker */}
      <div className="bg-black text-white overflow-hidden py-2 border-b-4 border-black">
        <div className="animate-marquee whitespace-nowrap flex gap-10">
          {[...Array(6)].map((_, i) => (
            <span key={i} className="font-pixel text-lg tracking-wider">
              {tickerContent}
            </span>
          ))}
        </div>
      </div>

      <nav className="p-6 flex justify-between items-center border-b-4 border-black dark:border-gray-600 bg-background dark:bg-gray-950">
        <div className="flex items-center gap-4">
          <h1 className="text-4xl font-black uppercase tracking-tighter animate-glitch">
            Moon<span className="text-accent">shot</span>
          </h1>
          {/* WS Status Indicator */}
          <div className="flex items-center gap-1.5" title={wsConnected ? 'WebSocket Connected' : 'WebSocket Disconnected'}>
            <span className={`inline-block w-3 h-3 rounded-full ${wsConnected ? 'bg-green-400 animate-pulse' : 'bg-red-500'}`} />
            <span className="text-[10px] font-mono text-muted-foreground uppercase">
              {wsConnected ? 'LIVE' : 'OFFLINE'}
            </span>
          </div>
        </div>

        <div className="flex gap-3 items-center">
          {(['dashboard', 'trades', 'summary'] as const).map((view) => (
            <button
              key={view}
              onClick={() => onViewChange(view)}
              className={`brut-border brut-shadow-hover px-4 py-2 font-bold uppercase text-sm ${
                activeView === view
                  ? 'bg-accent text-white shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] translate-x-1 translate-y-1'
                  : 'bg-white dark:bg-gray-800 text-black dark:text-white brut-shadow'
              }`}
            >
              {view === 'summary' ? '📊 Summary' : view.charAt(0).toUpperCase() + view.slice(1)}
            </button>
          ))}
          {/* Dark mode toggle */}
          <button
            onClick={onToggleDark}
            className="brut-border brut-shadow-hover px-3 py-2 bg-white dark:bg-gray-800 text-lg"
            title="Toggle dark mode"
          >
            {darkMode ? '☀️' : '🌙'}
          </button>
        </div>
      </nav>

      <main className="p-6 max-w-7xl mx-auto flex-grow w-full">
        {children}
      </main>

      <footer className="px-6 py-3 border-t-4 border-black bg-black text-white font-mono text-xs">
        {/* Row 1: Live 24H Gainers */}
        <div className="flex items-center gap-2 mb-2">
          <span className="text-yellow-400 font-bold text-sm shrink-0">🔥 24H TOP</span>
          <div className="flex gap-3 overflow-x-auto">
            {gainers.length === 0 ? (
              <span className="text-gray-500">Loading...</span>
            ) : (
              gainers.map((g, i) => (
                <div key={g.symbol} className="flex items-center gap-1.5 whitespace-nowrap shrink-0">
                  <span className="text-gray-500">#{i + 1}</span>
                  <span className="font-bold">{g.symbol.replace('USDT', '')}</span>
                  <span className="text-green-400 font-bold">+{g.pct_chg}%</span>
                  <span className="text-gray-500">${(parseFloat(g.price) || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}</span>
                  {i < gainers.length - 1 && <span className="text-gray-700 mx-0.5">|</span>}
                </div>
              ))
            )}
          </div>
        </div>
        {/* Row 2: Yesterday Scan Results */}
        <div className="flex items-center gap-2 border-t border-gray-800 pt-2">
          <span className="text-cyan-400 font-bold text-sm shrink-0">📡 SCAN</span>
          {scanResult?.scan_time && Array.isArray(scanResult.gainers) ? (
            <>
              <span className="text-gray-600 shrink-0">
                {scanResult.scan_time.slice(5, 10)} {scanResult.scan_time.slice(11, 16)}
              </span>
              <div className="flex gap-3 overflow-x-auto">
                {scanResult.gainers.map((g) => (
                  <div key={g.symbol} className="flex items-center gap-1 whitespace-nowrap shrink-0" title={g.detail}>
                    <span className="font-bold">{g.symbol.replace('USDT', '')}</span>
                    <span className="text-green-400">+{g.pct_chg}%</span>
                    {g.status === 'accepted' ? (
                      <span className="text-green-400">✓</span>
                    ) : (
                      <span className="text-red-400" title={g.detail}>✗ <span className="text-gray-600 text-[10px]">{g.detail}</span></span>
                    )}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <span className="text-gray-500">No scan data</span>
          )}
        </div>
      </footer>
    </div>
  );
};
