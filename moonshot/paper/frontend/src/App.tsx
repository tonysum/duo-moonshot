import { useState, useEffect, useRef } from 'react';
import { Layout } from './components/Layout';
import { PositionCard } from './components/PositionCard';
import { ControlPanel } from './components/ControlPanel';
import { TradesView } from './components/TradesView';
import { SummaryView } from './components/SummaryView';
import { createStream, fetchLogs, fetchPendingSt, fetchTopGainers, fetchScanResults } from './api';

function App() {
  const [currentView, setCurrentView] = useState<'dashboard' | 'trades' | 'summary'>('dashboard');
  const [status, setStatus] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [prices, setPrices] = useState<Record<string, number>>({});
  const [wsConnected, setWsConnected] = useState(false);
  const [logs, setLogs] = useState<any[]>([]);
  const [pendingSt, setPendingSt] = useState<any[]>([]);
  const [gainers, setGainers] = useState<any[]>([]);
  const [scanResult, setScanResult] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [darkMode, setDarkMode] = useState(() => localStorage.getItem('darkMode') === 'true');
  const prevTradeCount = useRef(0);

  // Initial fetch for footer data (warms backend cache, immediate display)
  useEffect(() => {
    fetchTopGainers().then(setGainers).catch(() => {});
    fetchScanResults().then(setScanResult).catch(() => {});
  }, []);

  // SSE stream for real-time positions + prices + status + footer data
  useEffect(() => {
    const es = createStream((data) => {
      if (data.error) return;
      setStatus(data.status);
      setPositions(data.positions || []);
      setPrices(data.prices || {});
      setWsConnected(data.ws_connected ?? false);
      setLoading(false);
      if (Array.isArray(data.top_gainers)) setGainers(data.top_gainers);
      if (data.scan_results !== undefined) setScanResult(data.scan_results);

      // Browser notification on new trade close
      const tc = data.status?.total_trades ?? 0;
      if (prevTradeCount.current > 0 && tc > prevTradeCount.current) {
        notify(`Trade closed! Total: ${tc}`);
      }
      prevTradeCount.current = tc;
    }, () => setWsConnected(false));
    return () => es.close();
  }, []);

  // Slower poll for logs + pending ST (every 15s)
  useEffect(() => {
    const load = async () => {
      try {
        const [l, pst] = await Promise.all([fetchLogs(20), fetchPendingSt()]);
        setLogs(l); setPendingSt(pst);
      } catch {}
    };
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, []);

  // Dark mode toggle
  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode);
    localStorage.setItem('darkMode', String(darkMode));
  }, [darkMode]);

  if (loading && !status) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background font-black text-4xl uppercase animate-blink">
        Initializing System...
      </div>
    );
  }

  return (
    <Layout
      activeView={currentView}
      onViewChange={setCurrentView}
      prices={prices}
      wsConnected={wsConnected}
      darkMode={darkMode}
      onToggleDark={() => setDarkMode(!darkMode)}
      gainers={gainers}
      scanResult={scanResult}
    >
      <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
        <div className="md:col-span-2">
          {currentView === 'dashboard' ? (
            <div className="space-y-8">
              <section className="brut-border brut-shadow bg-white dark:bg-gray-900 p-8">
                <h2 className="text-3xl font-black uppercase mb-6 flex items-center justify-between">
                  Status Overview
                  <span className={`text-sm font-mono px-3 py-1 brut-border ${status?.running ? 'bg-green-400' : 'bg-red-400'}`}>
                    {status?.running ? 'RUNNING' : 'STOPPED'}
                  </span>
                </h2>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
                  <div className="p-4 brut-border bg-slate-50 dark:bg-gray-800">
                    <p className="text-xs font-bold text-muted-foreground uppercase">Current Capital</p>
                    <p className="text-3xl font-black">${status?.capital?.toLocaleString(undefined, { minimumFractionDigits: 2 })}</p>
                  </div>
                  <div className="p-4 brut-border bg-slate-50 dark:bg-gray-800">
                    <p className="text-xs font-bold text-muted-foreground uppercase">Open Positions</p>
                    <p className="text-3xl font-black">{status?.open_positions}</p>
                  </div>
                  <div className="p-4 brut-border bg-slate-50 dark:bg-gray-800">
                    <p className="text-xs font-bold text-muted-foreground uppercase">Total Trades</p>
                    <p className="text-3xl font-black">{status?.total_trades}</p>
                  </div>
                </div>
              </section>

              <section>
                <h2 className="text-3xl font-black uppercase mb-6">Open Positions</h2>
                {positions.length === 0 ? (
                  <div className="brut-border border-dashed p-12 text-center text-muted-foreground font-bold uppercase">
                    No active positions found.
                  </div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
                    {positions.map((p) => (
                      <PositionCard key={p.symbol} {...p} />
                    ))}
                  </div>
                )}
              </section>

              <section>
                <h2 className="text-3xl font-black uppercase mb-6 flex items-center">
                  Pending Supertrend Confirmations
                  {pendingSt.length > 0 && (
                    <span className="ml-4 bg-yellow-300 text-black text-xs px-2 py-1 rounded brut-border">
                      {pendingSt.length} WAITING
                    </span>
                  )}
                </h2>
                {pendingSt.length === 0 ? (
                  <div className="brut-border border-dashed p-8 text-center text-muted-foreground font-bold uppercase text-sm">
                    No signals pending confirmation.
                  </div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    {pendingSt.map((st) => (
                      <div key={st.symbol} className="brut-border bg-yellow-50 dark:bg-yellow-900/30 p-4 relative overflow-hidden">
                        <div className="flex justify-between items-start mb-2">
                          <h3 className="text-xl font-black">{st.symbol}</h3>
                          <span className="text-emerald-600 font-bold bg-emerald-100 px-2 py-0.5 text-xs brut-border">
                            +{st.pct_chg?.toFixed(1)}%
                          </span>
                        </div>
                        <div className="text-sm bg-white dark:bg-gray-800 p-2 brut-border">
                          <span className="font-bold mr-2">Status:</span>
                          Waiting for Bearish Flip
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            </div>
          ) : currentView === 'trades' ? (
            <TradesView />
          ) : (
            <SummaryView />
          )}
        </div>

        {/* Sidebar */}
        <div className="space-y-8">
          <ControlPanel isRunning={status?.running} onRefresh={() => {}} />

          <div className="brut-border brut-shadow bg-white dark:bg-gray-900 p-6">
            <h3 className="text-xl font-black uppercase mb-4">System Events</h3>
            <div className="font-mono text-[10px] space-y-2 h-[500px] overflow-y-auto pr-2 divide-y border-t pt-2">
              {logs.map((log: any, i: number) => (
                <div key={i} className="py-2">
                  <span className="text-muted-foreground block text-[8px] mb-1">
                    {new Date(log.timestamp).toLocaleString()}
                  </span>
                  <span className={`font-bold mr-2 text-[9px] ${log.event_type === 'ENTRY' ? 'text-blue-600' :
                    log.event_type === 'EXIT' ? 'text-purple-600' :
                    log.event_type === 'SCAN' ? 'text-orange-600' : 'text-gray-600'
                  }`}>
                    [{log.event_type}]
                  </span>
                  {log.symbol && <span className="font-black mr-2">{log.symbol}</span>}
                  <span className="text-gray-700 dark:text-gray-300">{log.message}</span>
                </div>
              ))}
              {logs.length === 0 && <p className="text-muted-foreground">Waiting for events...</p>}
            </div>
          </div>
        </div>
      </div>
    </Layout>
  );
}

function notify(body: string) {
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification('Moonshot Trading', { body, icon: '🌙' });
  }
}

// Request notification permission on load
if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission();
}

export default App;
