# Duo-Moonshot 🌙

Duo-Moonshot is a standalone cryptocurrency trading engine for the **Moonshot（狙击top1）** strategy — backtesting, optimization, and real-time paper trading with a modern web dashboard.

## Features

- **Moonshot Strategy** — Advanced short-selling signal detection on high-momentum assets
- **Fast Backtesting** — Local PostgreSQL-powered historical simulation
- **Bayesian Optimization** — Automated parameter tuning via Optuna
- **Paper Trading** — Real-time simulation with Binance exchange data
  - WebSocket real-time price feed (~1s) with REST fallback
  - Trailing stop tracking
  - Real funding fee calculation from exchange
  - SSE live data push to frontend
- **Web Dashboard** — Neo-Brutalism React frontend
  - Real-time position cards with TP/SL distance, hold time, profit progress
  - Performance summary with per-symbol breakdown
  - CSV export, dark mode, mobile responsive
  - BTC/ETH/BNB/SOL price ticker
  - WebSocket connection status indicator
  - Browser notifications on trade close

## Structure

```
duo-moonshot/
├── pyproject.toml                # Dependencies (uv)
├── .env                          # Binance API keys & DB config
├── backtest.py                   # Backtest entry
├── deploy.sh                     # One-command deploy script
├── ecosystem.config.cjs          # PM2 process config
├── moonshot/                     # Core Engine
│   ├── models.py                 # Data models (Candle, Signal, Trade)
│   ├── strategy.py               # MoonshotStrategy & MoonshotConfig
│   ├── runner.py                 # Backtest runner (MoonshotRunner)
│   ├── optimizer.py              # Bayesian optimizer
│   ├── account.py                # Accounting logic
│   ├── data_feed.py              # PostgreSQL data pipeline
│   ├── db.py                     # Database connection
│   ├── client.py                 # Binance Futures REST client
│   ├── verify.py                 # CSV output verifier
│   └── paper/                    # Paper Trading Module
│       ├── __main__.py           # CLI (start / status / scan)
│       ├── api.py                # FastAPI endpoints + static hosting
│       ├── runner.py             # PaperRunner orchestrator
│       ├── paper_account.py      # Simulated account (real funding fees)
│       ├── paper_store.py        # SQLite persistence
│       ├── live_feed.py          # REST data feed + WS price cache
│       ├── ws_feed.py            # WebSocket mark price stream
│       ├── position_monitor.py   # Real-time exit checks (3s loop)
│       ├── daily_scanner.py      # Daily top gainer scanner
│       ├── supertrend_monitor.py # Multi-TF supertrend gating
│       └── frontend/             # React + Vite + Tailwind v4
│           ├── src/
│           │   ├── App.tsx        # Main app (SSE stream)
│           │   ├── api.ts        # API client + SSE helper
│           │   └── components/   # Layout, PositionCard, SummaryView, etc.
│           └── dist/             # Production build (served by FastAPI)
└── reports/                      # Backtest CSV results
```

## Quick Start

### 1. Install

```bash
uv sync
```

### 2. Configure

Copy `.env.example` to `.env`, set your PostgreSQL and Binance API credentials.

### 3. Backtest

```bash
uv run python backtest.py --start 2025-01-01 --end 2026-03-18
```

### 4. Optimize

```bash
uv run python -m moonshot.optimizer --trials 50
```

### 5. Paper Trading (Development)

```bash
# Backend + API
uv run python -m moonshot.paper start --port 8100

# Frontend dev server (separate terminal)
cd moonshot/paper/frontend && npm install && npm run dev
```

### 6. Deploy (Production)

```bash
# One-command deploy with PM2
bash deploy.sh

# Or manually:
cd moonshot/paper/frontend && npm run build && cd ../../..
pm2 start ecosystem.config.cjs
```

Dashboard: `http://SERVER_IP:8100`

## PM2 Commands

```bash
pm2 status                      # Check status
pm2 logs moonshot-paper         # View logs
pm2 restart moonshot-paper      # Restart
pm2 stop moonshot-paper         # Stop
```
