"""Duo-Moonshot R24 CLI — Backtest entry point for 24h rolling strategy.

Usage:
    python backtest_rolling.py --start 2025-01-01 --top_n 3
    python backtest_rolling.py --start 2025-01-01 --end 2025-03-20 --capital 10000 --cooldown 12
"""

import argparse
import logging
from datetime import datetime, timezone
from moonshot.db import get_postgres_db as get_db
from moonshot.rolling_data_feed import RollingDataFeed
from moonshot.account import Account
from moonshot.rolling_strategy import RollingStrategy, RollingConfig
from moonshot.rolling_runner import RollingRunner

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("moonshot-r24")


def main():
    parser = argparse.ArgumentParser(description="Duo-Moonshot R24 Backtest CLI (24h Rolling)")
    parser.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--top_n", type=int, default=3, help="Top N gainers to consider")
    parser.add_argument("--cooldown", type=int, default=24, help="Signal cooldown hours (default: 24)")
    parser.add_argument("--window", type=int, default=24, help="Rolling window hours (default: 24)")
    parser.add_argument("--scan-interval", type=int, default=1, help="Scan interval hours (default: 1, e.g. 4=every 4h)")
    parser.add_argument("--workers", type=int, default=0, help="Parallel workers for data loading (0=auto)")
    parser.add_argument("--no-csv", action="store_true", help="Skip CSV export")

    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)

    db = get_db()
    db.connect()
    feed = RollingDataFeed(db, workers=args.workers) if args.workers else RollingDataFeed(db)

    cfg = RollingConfig(top_n=args.top_n, signal_cooldown_hours=args.cooldown, rolling_window_hours=args.window, scan_interval_hours=args.scan_interval)
    strategy = RollingStrategy(config=cfg)
    account = Account(args.capital)

    runner = RollingRunner(feed=feed, account=account, strategy=strategy, verbose=True)

    print(f"\n{'='*50}")
    print(f"  🌙 Duo-Moonshot R24 Backtest (24h Rolling)")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"  Capital: ${args.capital:,.2f}")
    print(f"  Top N: {args.top_n}, Cooldown: {args.cooldown}h, Window: {args.window}h, Scan: every {args.scan_interval}h")
    print(f"{'='*50}\n")

    result = runner.run(start, end)

    # Export CSV
    if not args.no_csv:
        csv_path = runner.export_csv(result)

    # Print summary at the very end
    runner.print_summary(result)

    if not args.no_csv:
        print(f"\n📄 CSV exported: {csv_path}")

    db.close()


if __name__ == "__main__":
    main()
