import React from 'react';

export type ScanSnapshot = {
  scan_time: string;
  gainers: { symbol: string; pct_chg: number; status: string; detail: string }[];
};

/** 与 GET /stream 中 feed_health 对齐，用于顶栏/页脚展示连接与 API 错误 */
export type FeedHealthSnapshot = {
  mark_prices_ws: boolean;
  top_gainers_ok: boolean;
  top_gainers_error: string | null;
  top_gainers_error_at: string | null;
  top_gainers_last_ok_at: string | null;
};

export interface LayoutProps {
  children: React.ReactNode;
  activeView: 'dashboard' | 'trades' | 'summary';
  onViewChange: (view: 'dashboard' | 'trades' | 'summary') => void;
  prices: Record<string, number>;
  wsConnected: boolean;
  darkMode: boolean;
  onToggleDark: () => void;
  gainers?: { symbol: string; pct_chg: number; price: string; volume: number }[];
  /** Single-strategy mode: one SCAN row */
  scanResult?: ScanSnapshot | null;
  /** Dual mode: Daily + Rolling SCAN rows (omit in single mode) */
  scanResultsDual?: { daily: ScanSnapshot | null; rolling: ScanSnapshot | null };
  /** 后端 feed 健康（标记价 WS、24h 涨幅榜 REST 等）；无则仅显示绿/红 LIVE 点 */
  feedHealth?: FeedHealthSnapshot | null;
}

function ScanResultStrip({
  label,
  labelClassName,
  data,
}: {
  label: string;
  labelClassName: string;
  data: ScanSnapshot | null;
}) {
  return (
    <>
      <span className={`${labelClassName} font-bold text-sm shrink-0`}>{label}</span>
      {data?.scan_time && Array.isArray(data.gainers) ? (
        <>
          <span className="text-gray-600 shrink-0">
            {data.scan_time.slice(5, 10)} {data.scan_time.slice(11, 16)}
          </span>
          <div className="flex gap-3 overflow-x-auto">
            {data.gainers.map((g) => (
              <div key={g.symbol} className="flex items-center gap-1 whitespace-nowrap shrink-0" title={g.detail}>
                <span className="font-bold">{g.symbol.replace('USDT', '')}</span>
                {g.status === 'accepted' ? (
                  <>
                    <span className="text-green-400">+{g.pct_chg}%</span>
                    <span className="text-green-400">✓</span>
                  </>
                ) : (
                  <span className="text-red-400 text-[10px]">{g.detail}</span>
                )}
              </div>
            ))}
          </div>
        </>
      ) : (
        <span className="text-gray-500">No scan data</span>
      )}
    </>
  );
}

const TICKER_SYMBOLS = [
  { key: 'BTCUSDT', label: 'BTC', icon: '₿' },
  { key: 'ETHUSDT', label: 'ETH', icon: 'Ξ' },
  { key: 'BNBUSDT', label: 'BNB', icon: '◆' },
  { key: 'SOLUSDT', label: 'SOL', icon: '◎' },
];

export const Layout: React.FC<LayoutProps> = ({
  children, activeView, onViewChange, prices, wsConnected, darkMode, onToggleDark,
  gainers = [], scanResult = null, scanResultsDual, feedHealth = null,
}) => {
  const tickerContent = TICKER_SYMBOLS.map(({ key, label, icon }) => {
    const price = prices[key];
    return `${icon} ${label}: $${price ? price.toLocaleString(undefined, { minimumFractionDigits: 2 }) : '---'}`;
  }).join('     ·     ');

  const fh = feedHealth;
  const markOk = fh != null ? fh.mark_prices_ws : wsConnected;
  const showHealthAlert = fh && (!fh.mark_prices_ws || !fh.top_gainers_ok);
  const liveDetail =
    fh && !fh.mark_prices_ws
      ? '全市场标记价 WS 断开（监控会回退 REST，可能更慢）'
      : fh && !fh.top_gainers_ok
        ? '24H 涨幅榜拉取失败（见下方黄条/页脚）'
        : 'Binance 全市场 !markPrice@1s 已连接';
  const hasFeedIssues = showHealthAlert;

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
          <div
            className="flex flex-col gap-0.5"
            title={hasFeedIssues ? [liveDetail, fh && !fh.top_gainers_ok ? fh.top_gainers_error : ''].filter(Boolean).join(' — ') : liveDetail}
          >
            <div className="flex items-center gap-1.5" title={liveDetail}>
              <span
                className={`inline-block w-3 h-3 rounded-full ${
                  markOk && (fh == null || fh.top_gainers_ok) ? 'bg-green-400 animate-pulse' : 'bg-amber-500'
                }`}
              />
              <span className="text-[10px] font-mono text-muted-foreground uppercase">
                {markOk && (fh == null || fh.top_gainers_ok) ? 'LIVE' : 'DEGRADED'}
              </span>
            </div>
            {fh && (!fh.mark_prices_ws || !fh.top_gainers_ok) && (
              <span className="text-[9px] font-mono text-amber-600 dark:text-amber-400 max-w-[min(24rem,70vw)] truncate">
                {[!fh.mark_prices_ws && 'WS↓', !fh.top_gainers_ok && 'Top榜↓'].filter(Boolean).join(' ')}
              </span>
            )}
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

      {showHealthAlert && fh && (
        <div
          className="px-4 py-2 border-b-4 border-black bg-amber-400 text-black text-sm font-bold font-mono space-y-1"
          role="status"
        >
          {!fh.mark_prices_ws && (
            <p>⚠️ 全市场标记价 WebSocket 未连接：持仓未实现盈亏仍可用 REST 回退，但与实时行情可能有偏差。</p>
          )}
          {!fh.top_gainers_ok && (
            <p>
              ⚠️ 24H 涨幅榜接口失败{fh.top_gainers_error_at && `（${fh.top_gainers_error_at.slice(0, 19).replace('T', ' ')} UTC）`}：{' '}
              <span className="break-words font-normal">{fh.top_gainers_error || 'Unknown error'}</span>
            </p>
          )}
        </div>
      )}

      <main className="w-full max-w-[1920px] mx-auto flex-grow py-6 px-4 sm:px-6 lg:px-8">
        {children}
      </main>

      <footer className="px-6 py-3 border-t-4 border-black bg-black text-white font-mono text-xs">
        {/* Row 1: Live 24H Gainers */}
        <div className="flex items-center gap-2 mb-2">
          <span className="text-yellow-400 font-bold text-sm shrink-0">🔥 24H TOP</span>
          <div className="flex gap-3 overflow-x-auto">
            {gainers.length === 0 && feedHealth && !feedHealth.top_gainers_ok ? (
              <span className="text-amber-400" title={feedHealth.top_gainers_error || ''}>
                涨幅榜拉取失败（见上方黄条）— {String(feedHealth.top_gainers_error || '').slice(0, 80)}
                {String(feedHealth.top_gainers_error || '').length > 80 ? '…' : ''}
              </span>
            ) : gainers.length === 0 ? (
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
        {/* Scan rows: dual (Daily + Rolling) or single */}
        {scanResultsDual !== undefined ? (
          <>
            <div className="flex items-center gap-2 border-t border-gray-800 pt-2">
              <ScanResultStrip
                label="📡 DAILY"
                labelClassName="text-amber-400"
                data={scanResultsDual.daily}
              />
            </div>
            <div className="flex items-center gap-2 border-t border-gray-800 pt-2">
              <ScanResultStrip
                label="📡 ROLLING"
                labelClassName="text-cyan-400"
                data={scanResultsDual.rolling}
              />
            </div>
          </>
        ) : (
          <div className="flex items-center gap-2 border-t border-gray-800 pt-2">
            <ScanResultStrip label="📡 SCAN" labelClassName="text-cyan-400" data={scanResult} />
          </div>
        )}
      </footer>
    </div>
  );
};
