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
