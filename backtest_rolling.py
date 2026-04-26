"""Duo-Moonshot R24 CLI — Backtest entry point for 24h rolling strategy.

Usage:
    python backtest_rolling.py --start 2025-01-01 --top_n 3
    python backtest_rolling.py --start 2025-01-01 --end 2025-03-20 --capital 10000 --cooldown 12
    python backtest_rolling.py --sizing fixed_usd --fixed-invest 400
    python backtest_rolling.py --sizing equity_pct --size-ratio 0.04   # 总权益×ratio（旧逻辑）
"""

import argparse
import logging
from dataclasses import replace
from datetime import datetime, timezone
from moonshot.db import get_postgres_db as get_db
from moonshot.rolling_data_feed import RollingDataFeed
from moonshot.account import Account
from moonshot.rolling_strategy import RollingStrategy, RollingConfig
from moonshot.rolling_runner import RollingRunner
from moonshot.r24_config_load import load_rolling_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("moonshot-r24")


def _print_r24_run_params(cfg: RollingConfig, start: datetime, end: datetime, initial_capital: float) -> None:
    """Same banner as startup; repeat after summary for copy-paste / log archives."""
    print(f"\n{'='*50}")
    print("  🌙 Duo-Moonshot R24 Backtest (24h Rolling)")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"  Capital: ${initial_capital:,.2f}")
    print(
        f"  Top N: {cfg.top_n}, Cooldown: {cfg.signal_cooldown_hours}h, "
        f"Window: {cfg.rolling_window_hours}h, Scan: every {cfg.scan_interval_hours}h"
    )
    if cfg.position_sizing_mode == "fixed_usd":
        print(f"  Sizing: fixed ${cfg.fixed_invest_usd:,.2f}/trade")
    else:
        print(f"  Sizing: {cfg.position_sizing_mode}  ratio={cfg.position_size_ratio}")
    if cfg.enable_sell_surge_gate:
        print(
            f"  Sell surge gate: ON  threshold>={cfg.sell_surge_threshold}  max<={cfg.sell_surge_max:g}"
        )
    print(f"{'='*50}")


def main():
    parser = argparse.ArgumentParser(description="Duo-Moonshot R24 Backtest CLI (24h Rolling)")
    parser.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument(
        "--top_n",
        type=int,
        default=None,
        help="Top N gainers (default: RollingConfig.top_n)",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=None,
        help="Signal cooldown hours (default: RollingConfig.signal_cooldown_hours)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help="Rolling window hours (default: RollingConfig.rolling_window_hours)",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=None,
        help="Scan interval hours (default: RollingConfig.scan_interval_hours, e.g. 4=every 4h)",
    )
    parser.add_argument("--workers", type=int, default=0, help="Parallel workers for data loading (0=auto)")
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "R24 params JSON (optional). Default: MOONSHOT_R24_PARAMS env, then "
            "(moonshot package parent, then cwd) × "
            "config/r24_params.json → reports/optimizer/r24_phase3_best.json, else code defaults."
        ),
    )
    parser.add_argument("--no-csv", action="store_true", help="Skip CSV export")
    parser.add_argument(
        "--sizing",
        choices=["free_cash_pct", "equity_pct", "fixed_usd"],
        default=None,
        help="Position sizing (default: RollingConfig.position_sizing_mode)",
    )
    parser.add_argument(
        "--size-ratio",
        type=float,
        default=None,
        help="Override position_size_ratio (default: RollingConfig.position_size_ratio)",
    )
    parser.add_argument(
        "--fixed-invest",
        type=float,
        default=None,
        help="Fixed margin per trade (USD) when --sizing fixed_usd",
    )
    parser.add_argument(
        "--fixed-margin",
        type=float,
        default=None,
        help="Same as --fixed-invest (fixed USD margin per trade when --sizing fixed_usd)",
    )
    parser.add_argument(
        "--sell-surge",
        action="store_true",
        help="Enable sell-volume surge gate (AND with R24 top gainers; hm1l-aligned)",
    )
    parser.add_argument(
        "--sell-surge-threshold",
        type=float,
        default=None,
        metavar="X",
        help="Min surge ratio vs yesterday avg hourly sell (default: RollingConfig.sell_surge_threshold)",
    )
    parser.add_argument(
        "--sell-surge-max",
        type=float,
        default=None,
        metavar="X",
        help="Max surge ratio upper cap (default: RollingConfig.sell_surge_max)",
    )

    args = parser.parse_args()

    base = load_rolling_config(args.config)
    cfg = replace(
        base,
        top_n=args.top_n if args.top_n is not None else base.top_n,
        signal_cooldown_hours=args.cooldown if args.cooldown is not None else base.signal_cooldown_hours,
        rolling_window_hours=args.window if args.window is not None else base.rolling_window_hours,
        scan_interval_hours=args.scan_interval if args.scan_interval is not None else base.scan_interval_hours,
        position_sizing_mode=args.sizing if args.sizing is not None else base.position_sizing_mode,
        position_size_ratio=args.size_ratio if args.size_ratio is not None else base.position_size_ratio,
        fixed_invest_usd=(
            args.fixed_invest
            if args.fixed_invest is not None
            else (args.fixed_margin if args.fixed_margin is not None else base.fixed_invest_usd)
        ),
        enable_sell_surge_gate=True if args.sell_surge else base.enable_sell_surge_gate,
        sell_surge_threshold=(
            args.sell_surge_threshold if args.sell_surge_threshold is not None else base.sell_surge_threshold
        ),
        sell_surge_max=args.sell_surge_max if args.sell_surge_max is not None else base.sell_surge_max,
    )
    if cfg.position_sizing_mode == "fixed_usd" and (cfg.fixed_invest_usd is None or cfg.fixed_invest_usd <= 0):
        parser.error("--fixed-invest or --fixed-margin (>0) is required when --sizing fixed_usd")

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)

    db = get_db()
    db.connect()
    feed = RollingDataFeed(db, workers=args.workers) if args.workers else RollingDataFeed(db)
    strategy = RollingStrategy(config=cfg)
    account = Account(args.capital)

    runner = RollingRunner(feed=feed, account=account, strategy=strategy, verbose=True)

    _print_r24_run_params(cfg, start, end, args.capital)
    print()

    result = runner.run(start, end)

    # Export CSV
    if not args.no_csv:
        csv_path = runner.export_csv(result)

    runner.print_summary(result)
    _print_r24_run_params(cfg, start, end, result.initial_capital)

    if not args.no_csv:
        print(f"\n📄 CSV exported: {csv_path}")

    db.close()


if __name__ == "__main__":
    main()
