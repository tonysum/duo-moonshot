export const API_BASE = `http://${window.location.hostname}:8100`;

export async function fetchStatus() {
  const res = await fetch(`${API_BASE}/status`);
  return res.json();
}

export async function fetchPositions() {
  const res = await fetch(`${API_BASE}/positions`);
  return res.json();
}

export async function fetchTrades(limit: number = 50) {
  const res = await fetch(`${API_BASE}/trades?limit=${limit}`);
  return res.json();
}

export async function fetchLogs(limit: number = 100) {
  const res = await fetch(`${API_BASE}/logs?limit=${limit}`);
  return res.json();
}

export async function triggerScan() {
  const res = await fetch(`${API_BASE}/scan`, { method: "POST" });
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

export async function fetchPendingSt() {
  const res = await fetch(`${API_BASE}/pending_st`);
  return res.json();
}

export async function fetchSummary() {
  const res = await fetch(`${API_BASE}/summary`);
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

export async function fetchScanResults(): Promise<{scan_time: string; gainers: {symbol: string; pct_chg: number; status: string; detail: string}[]} | null> {
  const res = await fetch(`${API_BASE}/scan_results`);
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
