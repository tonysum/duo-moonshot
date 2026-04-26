"""R24 + 原始信号（滚动涨幅阈值 + 卖量倍数）回测入口 — 独立于 ``backtest_rolling.py``。

原始信号与 ``export_hourly_gainer_sell_surge`` / ``r24_raw_surge_preload`` 一致；
执行层（仓位、止盈止损、超时等）与 ``RollingRunner`` 相同。

用法::

    python backtest_rolling_raw_surge.py --start 2025-01-01 --end 2025-03-01
    python backtest_rolling_raw_surge.py --config config/r24_raw_surge_params.json

参数默认从 ``config/r24_raw_surge_params.json``（或环境变量 ``MOONSHOT_R24_RAW_SURGE_PARAMS``）加载；
命令行传入的 ``--raw-min-pct`` 等会覆盖 JSON 中的对应项。
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from moonshot.account import Account
from moonshot.db import get_postgres_db as get_db
from moonshot.r24_raw_surge_config import RawSurgeR24Config
from moonshot.r24_raw_surge_config_load import (
    get_last_loaded_raw_surge_config_path,
    load_raw_surge_r24_config,
)
from moonshot.r24_raw_surge_runner import R24RawSurgeRunner
from moonshot.r24_raw_surge_strategy import RawSurgeRollingStrategy
from moonshot.rolling_data_feed import RollingDataFeed

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("r24-raw-surge")


def _banner(cfg: RawSurgeR24Config, start: datetime, end: datetime, capital: float) -> None:
    print(f"\n{'='*50}")
    print("  Duo-Moonshot R24 + Raw Surge Signals (standalone)")
    print(f"  Period: {start.date()} to {end.date()}")
    print(f"  Capital: ${capital:,.2f}")
    print(
        f"  Raw signal: roll pct >= {cfg.min_pct_chg}%, "
        f"sell_surge > {cfg.raw_min_sell_surge}"
        f"{'' if cfg.raw_max_sell_surge is None else f' && <= {cfg.raw_max_sell_surge}'}"
        f" | window={cfg.rolling_window_hours}h | "
        f"rank={cfg.candidate_rank_mode} max_probe={cfg.max_sr_probe}"
    )
    if cfg.raw_max_signals_per_hour is not None:
        print(f"  Cap per hour: {cfg.raw_max_signals_per_hour} candidates")
    print(
        f"  Scan: every {cfg.scan_interval_hours}h | Cooldown: {cfg.signal_cooldown_hours}h | "
        f"Max pos: {cfg.max_positions}"
    )
    if cfg.position_sizing_mode == "fixed_usd":
        print(f"  Sizing: fixed ${cfg.fixed_invest_usd:,.2f}/trade")
    else:
        print(f"  Sizing: {cfg.position_sizing_mode}  ratio={cfg.position_size_ratio}")
    print(f"{'='*50}")


def main() -> None:
    p = argparse.ArgumentParser(description="R24 backtest with raw surge signal preload (new pipeline)")
    p.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD UTC")
    p.add_argument("--end", default=None, help="End date YYYY-MM-DD UTC")
    p.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    p.add_argument("--workers", type=int, default=0, help="Parallel workers for preload (0=auto)")
    p.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "Raw-surge R24 JSON (default: MOONSHOT_R24_RAW_SURGE_PARAMS, then "
            "config/r24_raw_surge_params.json under repo root or cwd)"
        ),
    )
    p.add_argument("--raw-min-pct", type=float, default=None, help="Override JSON: min rolling %% for raw signal")
    p.add_argument("--raw-min-sell-surge", type=float, default=None, help="Override JSON: sell surge strict >")
    p.add_argument("--raw-max-sell-surge", type=float, default=None, help="Override JSON: sell surge upper bound")
    p.add_argument("--raw-max-per-hour", type=int, default=None, help="Override JSON: max candidates per hour")
    p.add_argument("--window", type=int, default=None, help="Override JSON: rolling window hours")
    p.add_argument("--scan-interval", type=int, default=None, help="Override JSON: scan interval hours")
    p.add_argument(
        "--candidate-rank-mode",
        choices=["sr", "pct_log_sr"],
        default=None,
        help="Override JSON: top_n sort key after sell-surge gate (aligns with duo-live rolling)",
    )
    p.add_argument(
        "--max-sr-probe",
        type=int,
        default=None,
        help="Override JSON: max symbols per hour to compute sr before top_n (0=unlimited)",
    )
    p.add_argument("--no-csv", action="store_true", help="Skip CSV export")

    args = p.parse_args()

    base = load_raw_surge_r24_config(args.config)
    _cfg_src = get_last_loaded_raw_surge_config_path()
    if _cfg_src:
        print(f"  Config JSON: {_cfg_src}")
    else:
        print("  Config JSON: (未找到文件，使用 RawSurgeR24Config 代码默认值)")
    cfg = replace(
        base,
        min_pct_chg=args.raw_min_pct if args.raw_min_pct is not None else base.min_pct_chg,
        raw_min_sell_surge=args.raw_min_sell_surge if args.raw_min_sell_surge is not None else base.raw_min_sell_surge,
        raw_max_sell_surge=(
            args.raw_max_sell_surge if args.raw_max_sell_surge is not None else base.raw_max_sell_surge
        ),
        raw_max_signals_per_hour=(
            args.raw_max_per_hour if args.raw_max_per_hour is not None else base.raw_max_signals_per_hour
        ),
        rolling_window_hours=args.window if args.window is not None else base.rolling_window_hours,
        scan_interval_hours=args.scan_interval if args.scan_interval is not None else base.scan_interval_hours,
        candidate_rank_mode=(
            args.candidate_rank_mode if args.candidate_rank_mode is not None else base.candidate_rank_mode
        ),
        max_sr_probe=args.max_sr_probe if args.max_sr_probe is not None else base.max_sr_probe,
    )

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # 约定：--end 传入的是「最后一天（UTC）」而不是「00:00 截止时刻」。
    # Runner 内部按 [start, end] 小时循环并在 end 时刻强制平仓；若 end=YYYY-MM-DD 00:00
    # 会导致当日 00:05 才成交的仓位被 00:00 强平，从而出现 exit_time <= entry_time 的 verify 异常。
    end = (
        # 解释为「最后一天的结束时刻（23:59:59 UTC）」；避免进入次日 00:00 这一小时产生 00:05 成交，
        # 但循环结束后又用 end(00:00) 强平，导致 exit_time <= entry_time。
        datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        + timedelta(days=1)
        - timedelta(seconds=1)
        if args.end
        else datetime.now(timezone.utc)
    )

    db = get_db()
    db.connect()
    try:
        workers = args.workers if args.workers > 0 else None
        feed = RollingDataFeed(db, workers=workers) if workers else RollingDataFeed(db)
        strategy = RawSurgeRollingStrategy(config=cfg)
        account = Account(args.capital)
        runner = R24RawSurgeRunner(feed=feed, account=account, strategy=strategy, verbose=True)

        _banner(cfg, start, end, args.capital)
        print()

        result = runner.run(start, end)

        if not args.no_csv:
            csv_path = runner.export_csv(result, prefix="rolling_raw_surge")

        runner.print_summary(result)
        _banner(cfg, start, end, result.initial_capital)

        if not args.no_csv:
            print(f"\nCSV: {csv_path.resolve()}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
