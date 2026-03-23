export const API_BASE = `http://${window.location.hostname}:8100`;

export async function fetchStatus() {
  const res = await fetch(`${API_BASE}/status`);
  return res.json();
}

export async function fetchPositions(strategy?: 'daily' | 'rolling') {
  const q = strategy ? `?strategy=${strategy}` : '';
  const res = await fetch(`${API_BASE}/positions${q}`);
  return res.json();
}

export async function fetchTrades(limit: number = 50, strategy?: 'daily' | 'rolling') {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (strategy) qs.set('strategy', strategy);
  const res = await fetch(`${API_BASE}/trades?${qs}`);
  return res.json();
}

export async function fetchLogs(limit: number = 100, strategy?: 'daily' | 'rolling') {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (strategy) qs.set('strategy', strategy);
  const res = await fetch(`${API_BASE}/logs?${qs}`);
  return res.json();
}

export async function triggerScan(strategy?: 'daily' | 'rolling') {
  const q = strategy ? `?strategy=${strategy}` : '';
  const res = await fetch(`${API_BASE}/scan${q}`, { method: "POST" });
  return res.json();
}

export async function stopRunner() {
  const res = await fetch(`${API_BASE}/stop`, { method: "POST" });
  return res.json();
}

export async function startRunner() {
  const res = await fetch(`${API_BASE}/start`, { method: "POST" });
  return res.json();
}

export async function openPending(symbol: string) {
  const res = await fetch(`${API_BASE}/open_pending/${symbol}`, { method: "POST" });
  return res.json();
}

export async function fetchPendingSt(strategy?: 'daily' | 'rolling') {
  const q = strategy ? `?strategy=${strategy}` : '';
  const res = await fetch(`${API_BASE}/pending_st${q}`);
  return res.json();
}

export async function fetchSummary(strategy?: 'daily' | 'rolling') {
  const q = strategy ? `?strategy=${strategy}` : '';
  const res = await fetch(`${API_BASE}/summary${q}`);
  return res.json();
}

/** 与回测 moonshot/rolling_runner export_csv 列一致的已平仓 CSV（UTF-8 BOM），表尾可选 Summary 段 */
export async function downloadPaperTradesCsv(
  strategy?: 'daily' | 'rolling',
  includeSummary: boolean = true,
): Promise<void> {
  const qs = new URLSearchParams({ include_summary: String(includeSummary) });
  if (strategy) qs.set('strategy', strategy);
  const res = await fetch(`${API_BASE}/export/trades.csv?${qs}`);
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || res.statusText);
  }
  const blob = await res.blob();
  const dispo = res.headers.get('Content-Disposition');
  let fname = `paper_trades_${new Date().toISOString().slice(0, 10)}.csv`;
  const m = dispo?.match(/filename="?([^";]+)"?/i);
  if (m) fname = m[1].trim();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fname;
  a.click();
  URL.revokeObjectURL(url);
}

export async function fetchPrices(): Promise<Record<string, number>> {
  const res = await fetch(`${API_BASE}/prices`);
  return res.json();
}

export async function fetchTopGainers(): Promise<{symbol: string; pct_chg: number; price: string; volume: number}[]> {
  const res = await fetch(`${API_BASE}/top_gainers`);
  return res.json();
}

export async function fetchScanResults(strategy?: 'daily' | 'rolling'): Promise<any> {
  const q = strategy ? `?strategy=${strategy}` : '';
  const res = await fetch(`${API_BASE}/scan_results${q}`);
  return res.json();
}

// SSE stream for real-time data
export function createStream(
  onData: (data: any) => void,
  onError?: () => void
): EventSource {
  const es = new EventSource(`${API_BASE}/stream`);
  es.onmessage = (ev) => {
    try { onData(JSON.parse(ev.data)); } catch {}
  };
  es.onerror = () => { onError?.(); };
  return es;
}
