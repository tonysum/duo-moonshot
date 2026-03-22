# Duo-Moonshot 🌙

A standalone cryptocurrency trading engine for the **Moonshot（狙击top1）** strategy — targeting daily top gainers with short-selling, featuring backtesting, Bayesian optimization, and real-time paper trading with a live web dashboard.

## Strategy Overview

Moonshot monitors Binance USDT-M futures for **daily top gainers** (≥10% surge). When criteria are met, it opens **short positions** anticipating mean reversion. **Daily surge** = yesterday's full-day gain `(close - open) / open`, not close-to-close. The strategy includes multi-layer entry filters and flexible exit logic:

### Entry Filters (Gates)
| Gate | Name | Description |
|------|------|-------------|
| Signal | Top N Gainers | Selects top N (default 3) gainers with ≥10% daily surge (open→close) |
| 0 | 新币过滤 | Skips symbols listed less than 10 days (min_listed_days) |
| A | 主力获利检查 | Checks if the surge is supported by 30-day average price movement |
| B | Supertrend Gate | Waits for bearish Supertrend confirmation before entry |

### Exit Conditions
| Type | Default | Description |
|------|---------|-------------|
| Take Profit | 34% | Short profit target (drops to 14% after 10h hold) |
| Stop Loss | 44% | Maximum adverse movement |
| Trailing Stop | 9% from low | Activates at 16% profit, trails 9% from lowest price |
| Timeout | 11 days | Forced close after max hold period |
| Add Position | 36% adverse | Equal-size add if price moves against by 36%; TP becomes 45% |
| Dynamic SL | 多空比 | Optional: closes if long/short ratio drops ≥18% |

## Features

### Backtesting Engine
- PostgreSQL-powered historical data pipeline
- Full trade lifecycle simulation with realistic fees
- CSV export with detailed per-trade statistics
- Walk-Forward compatible time range splits

### Bayesian Optimizer
- Optuna-based multi-parameter optimization
- Penalizes unrealistic parameter combinations (excessive leverage, tight trailing stops)
- Walk-Forward validation for robustness

### Paper Trading System
- **Real-time exchange data** via Binance Futures REST + WebSocket
- **WebSocket mark price stream** (~1s updates) with multi-endpoint failover
- **Trailing stop tracking** — records lowest price for accurate trailing stop activation
- **Real funding fee calculation** — fetches actual funding rates from Binance for the hold period
- **3-second position monitoring loop** for fast exit reaction
- **Daily auto-scan** at 00:05 UTC for new signals
- **Multi-timeframe Supertrend gating** (optional, 15m/1h/4h)
- **SQLite persistence** — positions, trades, equity snapshots, event logs survive restarts
- **Detailed scan logging** — shows which symbols were found, filtered, and why
- **Top gainers for footer** — 24h gainers (TRADING-only, exclude SETTLING), 1h cache, refresh ~2 min past each hour
- **R24 rolling strategy** — `--strategy rolling` runs hourly 24h-rolling top gainer scan (loads config from `reports/optimizer/r24_phase3_best.json` if present)
- **Dual strategy mode** — `--strategy both` runs daily and rolling in parallel, each with independent 10k capital, Tab-switchable dashboard for comparison
- **Footer auto-update** — top gainers + last scan results pushed via SSE, no manual refresh needed

### Web Dashboard (Neo-Brutalism UI)
- **SSE real-time push** — positions + prices + status + top gainers + scan results every 2s (replaces REST polling)
- **Position cards** — hold time, TP/SL distance %, profit progress bar, lowest price, leverage badge
- **Performance summary** — win rate, profit factor, per-symbol breakdown, equity curve data
- **CSV export** — download summary report from browser
- **BTC/ETH/BNB/SOL price ticker** — real-time marquee
- **Footer** — 24H TOP gainers (rolling 24h) + last scan results with filter status
- **WebSocket status indicator** — green pulse (LIVE) / red (OFFLINE) in nav
- **Browser notifications** — desktop alert on trade close
- **Dark mode** — toggle with localStorage persistence
- **Mobile responsive** — auto-adapting nav and layout
- **LAN accessible** — access from any device on the same network

### CLI (7 Commands)
```
python -m moonshot.paper start                     # Daily strategy (00:05 UTC)
python -m moonshot.paper start --strategy rolling  # R24 hourly 24h-rolling
python -m moonshot.paper start --strategy both     # Run daily + rolling in parallel (对照)
python -m moonshot.paper status                    # Default: paper_trading.db
python -m moonshot.paper status --dual             # Daily + rolling DBs
python -m moonshot.paper status --store daily      # paper_trading_daily.db only
python -m moonshot.paper positions --dual          # Same --dual / --store for positions|trades|logs|summary
python -m moonshot.paper positions       # Detailed open positions
python -m moonshot.paper trades          # Recent trades with fee breakdown
python -m moonshot.paper logs            # System events (--type SCAN/OPEN/CLOSE)
python -m moonshot.paper summary         # Full performance report
python -m moonshot.paper scan                      # Manual scan (--strategy daily|rolling|both)
python -m moonshot.paper reset --confirm           # Clear all data
```

## Project Structure

```
duo-moonshot/
├── backtest.py                   # Backtest CLI (--start, --end, --capital, --top_n)
├── backtest_rolling.py           # R24 Rolling backtest (hourly scan, K1h-based 24h change)
├── pyproject.toml                # Dependencies (uv)
├── .env                          # Binance API keys & PostgreSQL config
├── deploy.sh                     # One-command production deploy
├── ecosystem.config.cjs          # PM2 process management config
├── moonshot/
│   ├── strategy.py               # MoonshotConfig (all parameters) + MoonshotStrategy
│   ├── runner.py                 # MoonshotRunner (backtest executor)
│   ├── optimizer.py              # Bayesian optimizer (Optuna)
│   ├── models.py                 # Candle, Signal, AmplitudeTrade
│   ├── account.py                # Simulated accounting
│   ├── data_feed.py              # PostgreSQL data pipeline
│   ├── db.py                     # Database connection
│   ├── client.py                 # Binance Futures REST client
│   ├── verify.py                 # Backtest CSV verifier
│   ├── rolling_strategy.py       # R24: hourly 24h-rolling top gainer strategy
│   ├── rolling_data_feed.py      # R24: K1h-based 24h change preload
│   ├── rolling_runner.py         # R24: backtest runner
│   ├── rolling_optimizer.py      # R24: Bayesian optimizer
│   └── paper/                    # Paper Trading Module
│       ├── __main__.py           # CLI entry (7 commands)
│       ├── api.py                # FastAPI (REST + SSE + static hosting)
│       ├── runner.py             # PaperRunner + DualPaperRunner (4–6 async loops)
│       ├── paper_account.py      # Virtual account (real funding fees)
│       ├── paper_store.py        # SQLite persistence layer
│       ├── live_feed.py          # REST data + WS price cache adapter
│       ├── ws_feed.py            # WebSocket mark price stream (multi-URL failover)
│       ├── position_monitor.py   # Exit checks (3s loop, trailing stop)
│       ├── daily_scanner.py      # Top gainer scanner (with filter logging)
│       ├── supertrend_monitor.py # Multi-TF Supertrend gate
│       └── frontend/             # React + Vite + Tailwind v4
│           ├── src/
│           │   ├── App.tsx       # SSE stream + notifications + dark mode
│           │   ├── api.ts        # API client (dynamic hostname for LAN)
│           │   └── components/
│           │       ├── Layout.tsx       # Nav, ticker, WS indicator, dark toggle
│           │       ├── PositionCard.tsx # Hold time, TP/SL, progress bar
│           │       ├── SummaryView.tsx  # Stats + CSV export
│           │       ├── TradesView.tsx   # Trade history table
│           │       └── ControlPanel.tsx # Start/stop/scan controls
│           └── dist/             # Production build → served by FastAPI at :8100
└── reports/                      # Backtest CSV output
```

## Quick Start

### 1. Install

```bash
uv sync                                          # Python deps
cd moonshot/paper/frontend && npm install && cd ../../..  # Frontend deps (optional)
```

### 2. Configure

Copy `.env.example` → `.env`, fill in:
- `BINANCE_API_KEY` / `BINANCE_API_SECRET` — for paper trading
- `DATABASE_URL` — PostgreSQL connection for backtesting

### 3. Backtest

```bash
# Daily strategy (K1d, scan at 00:05 UTC)
uv run python backtest.py --start 2025-01-01 --end 2026-03-18
uv run python backtest.py --start 2025-06-01 --capital 20000 --top_n 5 --no-csv

# Rolling R24 (K1h, hourly scan, 24h rolling change)
uv run python backtest_rolling.py --start 2025-01-01 --end 2025-03-20 --top_n 3 --scan-interval 1
```

**Position sizing** (`backtest.py` / `backtest_rolling.py`): default is **剩余现金 × ratio** (`--sizing free_cash_pct`, ratio `0.04`). Use `--sizing equity_pct` for **总权益 × ratio** (capped by free cash, old behavior). Use `--sizing fixed_usd --fixed-invest 400` for a **fixed margin per trade** (capped by remaining cash). Optional: `--size-ratio 0.05`.

### 4. Optimize

```bash
uv run python -m moonshot.optimizer --trials 100
# R24 rolling optimizer — same sizing flags as backtest_rolling (default free_cash_pct)
uv run python -m moonshot.rolling_optimizer --phase 1 --trials 60
uv run python -m moonshot.rolling_optimizer --sizing equity_pct --phase 3 --trials 80
```

### 5. Paper Trading

```bash
# Development (two terminals)
uv run python -m moonshot.paper start --port 8100
cd moonshot/paper/frontend && npm run dev         # http://localhost:5173

# Production (single process, single port)
cd moonshot/paper/frontend && npm run build && cd ../../..
uv run python -m moonshot.paper start --port 8100  # http://SERVER_IP:8100
```

### 6. Deploy with PM2

进程通过 `scripts/paper-pm2.sh` 启动，从 `ecosystem.config.cjs` 里的 `env` 读取 **`PAPER_PORT`**（默认 `8100`）和 **`PAPER_STRATEGY`**：`daily` | `rolling` | `both`。改策略或端口后执行 **`pm2 restart moonshot-paper --update-env`** 即可生效。

```bash
bash deploy.sh   # installs deps, builds frontend, starts PM2

# Or manually:
pm2 start ecosystem.config.cjs
pm2 save
```

云服务器请放行安全组/防火墙中的 **`PAPER_PORT`**；可选 Nginx 反代到该端口。

## PM2 Commands

```bash
pm2 status                       # Check process status
pm2 logs moonshot-paper          # Live log stream
pm2 restart moonshot-paper --update-env   # Restart (pick up env changes)
pm2 stop moonshot-paper          # Stop
```

## Notes

- **Daily gain formula**: Signal uses yesterday's full-day gain `(close - open) / open`, not close-to-close. Re-run backtests after any formula changes.
- **Top gainers (footer)**: Uses rolling 24h from Binance ticker; only TRADING symbols (excludes SETTLING/delisting).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Browser (any device on LAN)                            │
│  ├── SSE EventSource ← /stream (2s push)                │
│  ├── REST → /trades, /logs, /summary, /pending_st       │
│  └── Browser Notification API                           │
└─────────────────┬───────────────────────────────────────┘
                  │ HTTP :8100
┌─────────────────▼───────────────────────────────────────┐
│  FastAPI (api.py)                                       │
│  ├── REST endpoints (status, positions, trades, ...)   │
│  ├── SSE /stream (positions, prices, status, top_gainers, scan_results) │
│  ├── Static file serving (frontend dist/)               │
│  └── CORS middleware                                    │
└─────────────────┬───────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────┐
│  PaperRunner (runner.py) — 4 async loops                │
│  ├── PositionMonitor (3s)  — exit checks + trailing     │
│  ├── DailyScanner (00:05 UTC) — top gainer detection    │
│  ├── SupertrendMonitor (60s) — ST gate checks           │
│  └── EquityLoop (300s) — equity snapshots               │
└───────┬─────────────────┬───────────────────────────────┘
        │                 │
┌───────▼───────┐ ┌───────▼───────┐
│ PriceFeedWS   │ │ LiveFeed      │
│ (ws_feed.py)  │ │ (live_feed.py)│
│ Mark prices   │ │ REST fallback │
│ ~1s updates   │ │ Klines, ratio │
│ Multi-URL     │ │ Funding rates │
└───────────────┘ └───────────────┘
        │                 │
        └────────┬────────┘
                 ▼
         Binance Futures API
```
