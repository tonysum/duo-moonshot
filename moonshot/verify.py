"""Verify backtest / paper-export trade CSVs.

- **DB**：交易对是否在库中有日 K（``load_listing_date`` 非空）。
- **PnL**：按 ``moonshot/account.py`` 与导出列逐笔复核盈亏，揪出算术或导出错误。
- **OHLC（--ohlc）**：与 PostgreSQL 内 K 线逐笔对照（需连库）。规则对齐 ``RollingRunner``：
  无补仓时 **开仓价** = 建仓时刻 K1h ``close``；有补仓时 **不核对** 首仓均价与 K1h，但 **核对** ``补仓时间`` 对应 K5m 上 ``high`` 须触及补仓限价（空头逆势补仓语义）。
  **平仓价** 按空头语义与当根 K5m 的可达性校验（含补仓单）。
  **超时**：日切 ``23:59:59`` 用当日全日 K1h 末收，否则用 ``end-1h～end`` 窗口末收。
- **资金守恒**（可选）：``--initial-capital`` + 最后一笔「余额」应对上 ``initial + sum(盈亏金额)``（全部已平仓、无在途保证金时差）。

高收益回测本身可能真实（复利 + 高杠杆 + 小币波动），但若 ``verify`` 只过「符号检查」说明不了问题；
PnL 核对能区分「算错」与「策略在假设下极强」。
"""

from __future__ import annotations

import argparse
import csv
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from moonshot.account import DEFAULT_COMMISSION_RATE, DEFAULT_SLIPPAGE_PCT
from moonshot.data_feed import DataFeed
from moonshot.db import get_postgres_db as get_db
from moonshot.models import Candle

# 与 Account.close_position_bt8 一致：双边 (commission + slippage) × 2
_FEE_RATE_RT = (DEFAULT_COMMISSION_RATE + DEFAULT_SLIPPAGE_PCT) * 2


class _OhlcStatsFeed(DataFeed):
    """包装 DataFeed：统计真实下发到 Postgres 的查询次数（K1h 无缓存；K5m 仅未命中缓存时计一次）。"""

    def __init__(self, db) -> None:
        super().__init__(db)
        self.exec_1h = 0
        self.exec_5m = 0

    def load_1h(self, symbol: str, start: datetime, end: datetime):  # type: ignore[override]
        self.exec_1h += 1
        return super().load_1h(symbol, start, end)

    def load_5m(self, symbol: str, start: datetime, end: datetime):  # type: ignore[override]
        key = (symbol, start, end)
        if key not in self._cache_5m:
            self.exec_5m += 1
        return super().load_5m(symbol, start, end)


def _is_bt8_export(fieldnames: list[str] | None) -> bool:
    if not fieldnames:
        return False
    return "盈亏金额" in fieldnames and "仓位大小" in fieldnames and "建仓价格" in fieldnames


def _f(x: Any) -> float:
    if x is None or x == "":
        return 0.0
    return float(x)


def _parse_dt_utc(s: str) -> datetime:
    s = (s or "").strip()
    if not s:
        raise ValueError("empty datetime")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"unrecognized datetime: {s!r}")


def _price_close(a: float, b: float, *, rel_tol: float, abs_tol: float) -> bool:
    if a == b:
        return True
    m = max(abs(a), abs(b), 1e-12)
    return abs(a - b) <= max(abs_tol, rel_tol * m)


def _price_in_bar(px: float, bar: Candle, *, rel_tol: float, abs_tol: float) -> bool:
    """成交价在棒内 [low, high]（含容差）；仅适用于成交价即 K 内成交价的语义。"""
    lo, hi = min(bar.low, bar.high), max(bar.low, bar.high)
    ref = max(abs(px), abs(hi), abs(lo), 1e-12)
    margin = max(abs_tol, rel_tol * ref, 2e-4 * ref)
    return (lo - margin) <= px <= (hi + margin)


def _add_touch_short(bar: Candle, add_px: float, *, rel_tol: float, abs_tol: float) -> bool:
    """空头补仓：``check_exit`` 在涨幅触及 ``add_position_threshold`` 时补仓，收盘价为 ``entry*(1+thresh)``。

    须有 ``candle_high >= add_px``（模型价），同止盈一样允许限价优于棒内极值之外的舍入误差。
    """
    _, hi = min(bar.low, bar.high), max(bar.low, bar.high)
    ref = max(abs(add_px), abs(hi), 1e-12)
    margin = max(abs_tol, rel_tol * ref, 2e-4 * ref)
    return hi + margin >= add_px


def _exit_constraints_short(bar: Candle, exit_px: float, reason: str, *, rel_tol: float, abs_tol: float) -> bool:
    """按 ``check_exit`` 语义核对空头平仓价与 5m 棒的可达性（非「必须在 OHLC 区间内」）。"""
    lo, hi = min(bar.low, bar.high), max(bar.low, bar.high)
    ref = max(abs(exit_px), abs(hi), abs(lo), 1e-12)
    margin = max(abs_tol, rel_tol * ref, 2e-4 * ref)
    if "止损" in reason or "动态" in reason:
        return hi + margin >= exit_px
    if "追踪" in reason:
        return hi + margin >= exit_px
    # 止盈族：触发条件为 low 足够低；模型填价为限价，可优于当时最深 low，不要求 exit 落在 [low,high]
    if "止盈" in reason or reason == "止盈":
        return lo - margin <= exit_px
    return _price_in_bar(exit_px, bar, rel_tol=rel_tol, abs_tol=abs_tol)


def _is_added_position(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in ("true", "1", "yes", "是")


def _same_5m_bucket(a: datetime, b: datetime) -> bool:
    """与 Binance 5m K 对齐：同一 UTC 日期且小时、分钟相同。"""
    return a.date() == b.date() and a.hour == b.hour and a.minute == b.minute


def _pick_exit_5m_bar(candles: list[Candle], exit_t: datetime) -> Candle | None:
    matches = [b for b in candles if _same_5m_bucket(b.open_time, exit_t)]
    if not matches:
        return None
    matches.sort(key=lambda b: abs((b.open_time - exit_t).total_seconds()))
    return matches[0]


def verify_ohlc_vs_db(
    path: Path,
    feed: DataFeed,
    *,
    price_rel_tol: float = 1e-5,
    price_abs_tol: float = 1e-12,
) -> tuple[int, int, int, list[str]]:
    """核对开仓价=建仓时刻 K1h close（无补仓）；补仓价=补仓时刻 K5m 上沿可达；
    平仓价按空头语义与平仓时刻 K5m（超时用 K1h）。

    有补仓时**不**核对「建仓价格」与首根 K1h（该列为均价）。

    返回 (通过笔数, 含补仓笔数, 总笔数, 失败说明)。
    """
    fails: list[str] = []
    ok = n_with_add = total = 0
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(row for row in f if not row.startswith("#"))
        if not _is_bt8_export(reader.fieldnames):
            return 0, 0, 0, ["Not a BT8 trades CSV."]
        for i, row in enumerate(reader, start=2):
            sym = (row.get("交易对") or "").strip()
            if not sym:
                continue
            total += 1
            try:
                entry_t = _parse_dt_utc(row.get("建仓时间") or "")
                exit_t = _parse_dt_utc(row.get("平仓时间") or "")
                entry_px = _f(row.get("建仓价格"))
                exit_px = _f(row.get("平仓价格"))
            except (TypeError, ValueError) as e:
                fails.append(f"行 {i} {sym}: 时间或价格解析失败 {e}")
                continue

            add_pos = _is_added_position(row.get("是否补仓"))
            if add_pos:
                n_with_add += 1
                try:
                    add_t = _parse_dt_utc(row.get("补仓时间") or "")
                    add_px = _f(row.get("补仓价格"))
                except (TypeError, ValueError) as e:
                    fails.append(f"行 {i} {sym}: 补仓时间/价格解析失败 {e}")
                    continue
                if add_px <= 0:
                    fails.append(f"行 {i} {sym}: 已补仓但补仓价格无效")
                    continue
                c5a = feed.load_5m(sym, add_t - timedelta(hours=1), add_t + timedelta(minutes=10))
                bar_a = _pick_exit_5m_bar(c5a, add_t)
                if bar_a is None:
                    fails.append(f"行 {i} {sym}: 补仓时刻无匹配 K5m 棒 {add_t}")
                    continue
                ref_a = max(add_px, entry_px, 1e-12)
                mid_a = (bar_a.high + bar_a.low) / 2.0
                if mid_a < 0.2 * ref_a or mid_a > 5.0 * ref_a:
                    fails.append(
                        f"行 {i} {sym}: 补仓K5m[{bar_a.low},{bar_a.high}] 与补仓价量级不符 (ref≈{ref_a:.6g})"
                    )
                    continue
                if not _add_touch_short(bar_a, add_px, rel_tol=price_rel_tol, abs_tol=price_abs_tol):
                    fails.append(
                        f"行 {i} {sym}: 补仓价 CSV={add_px} 与K5m不符(须 high≥限价) "
                        f"high={bar_a.high} open_time={bar_a.open_time}"
                    )
                    continue
            else:
                c1 = feed.load_1h(sym, entry_t, entry_t)
                if not c1:
                    fails.append(f"行 {i} {sym}: 建仓时刻无 K1h 数据 {entry_t}")
                    continue
                db_close = c1[0].close
                if not _price_close(db_close, entry_px, rel_tol=price_rel_tol, abs_tol=price_abs_tol):
                    fails.append(
                        f"行 {i} {sym}: 开仓价 CSV={entry_px} 库K1h收盘={db_close} @ {entry_t}"
                    )
                    continue

            reason = (row.get("平仓原因") or "").strip()
            if "超时" in reason:
                # RollingRunner：日末 timeout 用「当天 00:00～次日 00:00」全部 1h 末根收盘；
                # 回测结束强平用 ``load_1h(end-1h, end)``。
                if exit_t.hour == 23 and exit_t.minute >= 58:
                    day0 = exit_t.replace(hour=0, minute=0, second=0, microsecond=0)
                    c1x = feed.load_1h(sym, day0, day0 + timedelta(days=1))
                else:
                    c1x = feed.load_1h(sym, exit_t - timedelta(hours=1), exit_t)
                if not c1x:
                    c1x = feed.load_1h(sym, exit_t - timedelta(hours=6), exit_t + timedelta(hours=2))
                if not c1x:
                    fails.append(f"行 {i} {sym}: 超时平仓时刻附近无 K1h {exit_t}")
                    continue
                last_c = c1x[-1].close
                t_tol = max(
                    price_abs_tol,
                    price_rel_tol * max(abs(last_c), abs(exit_px), 1e-12),
                    5e-4 * max(abs(last_c), abs(exit_px), 1e-12),
                )
                if not _price_close(last_c, exit_px, rel_tol=price_rel_tol, abs_tol=t_tol):
                    fails.append(
                        f"行 {i} {sym}: 超时平仓价 CSV={exit_px} 库K1h末根收盘≈{last_c} @ {exit_t}"
                    )
                    continue
            else:
                c5 = feed.load_5m(sym, exit_t - timedelta(hours=1), exit_t + timedelta(minutes=10))
                bar = _pick_exit_5m_bar(c5, exit_t)
                if bar is None:
                    fails.append(f"行 {i} {sym}: 平仓时刻无匹配 K5m 棒 {exit_t}（按 UTC 时分对齐）")
                    continue
                ref_px = max(_f(row.get("建仓价格")), exit_px, 1e-12)
                mid = (bar.high + bar.low) / 2.0
                if mid < 0.2 * ref_px or mid > 5.0 * ref_px:
                    fails.append(
                        f"行 {i} {sym}: K5m 价区[{bar.low},{bar.high}] 与建仓/平仓价量级不符 "
                        f"(ref≈{ref_px:.6g})，疑似错棒或库数据异常"
                    )
                    continue
                if not _exit_constraints_short(bar, exit_px, reason, rel_tol=price_rel_tol, abs_tol=price_abs_tol):
                    fails.append(
                        f"行 {i} {sym}: 平仓价 CSV={exit_px} 与K5m[{bar.low},{bar.high}] "
                        f"不符门控语义({reason}) open_time={bar.open_time}"
                    )
                    continue

            ok += 1
    return ok, n_with_add, total, fails


def _expected_profit_amount(
    position_size: float,
    entry_price: float,
    exit_price: float,
    leverage: float,
    funding_fee_cost: float,
) -> float:
    invest = position_size * entry_price
    if entry_price <= 0 or invest <= 0:
        return 0.0
    actual_pct = (entry_price - exit_price) / entry_price * 100.0
    leveraged_pct = actual_pct * leverage
    gross = invest * leveraged_pct / 100.0
    fees = invest * _FEE_RATE_RT
    return gross - funding_fee_cost - fees


def verify_bt8_pnl(path: Path, tol_abs: float = 1.0) -> tuple[int, int, list[str]]:
    """逐笔核对盈亏金额与 BT8 公式。返回 (匹配行数, 总行数, 失败信息列表)。"""
    fails: list[str] = []
    ok = total = 0
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(row for row in f if not row.startswith("#"))
        if not _is_bt8_export(reader.fieldnames):
            return 0, 0, ["Not a BT8 trades CSV (missing 盈亏金额/仓位大小/建仓价格)."]
        for i, row in enumerate(reader, start=2):
            sym = (row.get("交易对") or "").strip()
            if not sym:
                continue
            total += 1
            try:
                size = _f(row.get("仓位大小"))
                entry = _f(row.get("建仓价格"))
                exit_px = _f(row.get("平仓价格"))
                lev = _f(row.get("杠杆倍数")) or 1.0
                reported = _f(row.get("盈亏金额"))
                funding = _f(row.get("资金费成本"))
                pct_col = _f(row.get("盈亏百分比"))
            except (TypeError, ValueError) as e:
                fails.append(f"行 {i} {sym}: 数值解析失败 {e}")
                continue

            expected = _expected_profit_amount(size, entry, exit_px, lev, funding)
            row_tol = max(tol_abs, 1e-4 * max(abs(expected), abs(reported), 1.0))
            if abs(reported - expected) > row_tol:
                fails.append(
                    f"行 {i} {sym}: 盈亏金额 报表={reported:.2f} 按公式≈{expected:.2f} "
                    f"(invest≈{size * entry:.2f} lev={lev:g})"
                )
                continue

            raw_pct = (entry - exit_px) / entry if entry else 0.0
            if pct_col != 0.0 and abs(raw_pct - pct_col) > 2e-4 and abs(raw_pct - pct_col) / max(abs(pct_col), 1e-9) > 0.02:
                fails.append(
                    f"行 {i} {sym}: 盈亏百分比 列={pct_col:.6f} 由价算={(entry - exit_px) / entry:.6f}"
                )
                continue

            ok += 1
    return ok, total, fails


def verify_symbol_db(path: Path) -> tuple[int, int, int]:
    """返回 (有 listing 的行数, listing 为 None 行数, 解析失败行数)。"""
    has_ld = missing = bad = 0
    db = get_db()
    db.connect()
    feed = DataFeed(db)
    try:
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(row for row in f if not row.startswith("#"))
            for row in reader:
                symbol = row.get("Symbol") or row.get("交易对")
                if not symbol:
                    continue
                t_ref = (
                    row.get("建仓时间")
                    or row.get("信号时间")
                    or row.get("信号日期")
                    or row.get("Date")
                )
                if not t_ref:
                    bad += 1
                    continue
                ld = feed.load_listing_date(symbol.strip())
                if ld is None:
                    missing += 1
                else:
                    has_ld += 1
    finally:
        db.close()
    return has_ld, missing, bad


def verify_capital_sum(path: Path, initial_capital: float, tol_abs: float = 5.0) -> tuple[float, float, float, bool]:
    """``initial + sum(盈亏)`` 与最后一笔「余额」是否在容差内。

    导出列「余额」对应 ``Account._running_capital``（**现金**，不含已占用保证金）。
    多笔持仓并行时，平仓后现金仍可能低于「初始+累计盈亏」，故本检查**仅作弱参考**。
    """
    total_pnl = 0.0
    final_bal: float | None = None
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(row for row in f if not row.startswith("#"))
        for row in reader:
            total_pnl += _f(row.get("盈亏金额"))
            b = row.get("余额")
            if b not in (None, ""):
                final_bal = _f(b)
    if final_bal is None:
        return total_pnl, initial_capital, 0.0, False
    expected_final = initial_capital + total_pnl
    ok = abs(expected_final - final_bal) <= tol_abs
    return total_pnl, expected_final, final_bal, ok


def verify(
    csv_path: str,
    *,
    initial_capital: float | None,
    skip_db: bool,
    skip_pnl: bool,
    pnl_tol: float,
    ohlc: bool,
    ohlc_tol: float,
    timing: bool = False,
):
    path = Path(csv_path)
    if not path.exists():
        print(f"❌ File not found: {path}")
        return 1

    print(f"📋 Verifying {path.name}...\n")

    with open(path, encoding="utf-8-sig") as f:
        f.readline()
        f.seek(0)
        reader = csv.DictReader(row for row in f if not row.startswith("#"))
        fieldnames = reader.fieldnames

    exit_code = 0

    if not skip_pnl and _is_bt8_export(list(fieldnames) if fieldnames else []):
        ok, total, fails = verify_bt8_pnl(path, tol_abs=pnl_tol)
        print("── BT8 盈亏公式核对（与 account.close_position_bt8 一致）──")
        if total == 0:
            print(f"  ⚠️  {fails[0] if fails else '无数据行'}")
            exit_code = 1
        else:
            print(f"  匹配 {ok}/{total} 笔（容差 ${pnl_tol:g}）")
            if fails:
                exit_code = 1
                for msg in fails[:25]:
                    print(f"  ❌ {msg}")
                if len(fails) > 25:
                    print(f"  … 另有 {len(fails) - 25} 条")
            else:
                print("  ✅ 逐笔盈亏与公式一致")
        print()

    if ohlc:
        if skip_db:
            print("❌ --ohlc 需要连接数据库，请勿同时使用 --skip-db\n")
            return 1
        db = get_db()
        db.connect()
        try:
            feed: DataFeed = _OhlcStatsFeed(db) if timing else DataFeed(db)
            t0 = time.perf_counter()
            ok_o, n_add, total_o, o_fails = verify_ohlc_vs_db(
                path, feed, price_rel_tol=ohlc_tol, price_abs_tol=max(1e-12, ohlc_tol * 1e-8)
            )
            ohlc_s = time.perf_counter() - t0
            print("── OHLC 与库内 K 线对照（R24：K1h 首开、K5m 补仓/平仓）──")
            if timing and isinstance(feed, _OhlcStatsFeed):
                n_sql = feed.exec_1h + feed.exec_5m
                print(
                    f"  ⏱ 本段 {ohlc_s:.2f}s｜Postgres 执行约 {n_sql} 次 SELECT"
                    f"（K1h×{feed.exec_1h}，K5m 未命中缓存×{feed.exec_5m}；"
                    f"同名同窗口 K5m 会走内存缓存）"
                )
            if total_o == 0:
                print(f"  ⚠️  {o_fails[0] if o_fails else '无数据'}")
                exit_code = 1
            else:
                n_plain = total_o - n_add
                print(
                    f"  通过 {ok_o}/{total_o} 笔（无补仓 {n_plain} 笔：核对首仓 K1h + 平仓；"
                    f"含补仓 {n_add} 笔：另核对补仓 K5m + 平仓，首仓均价不对照 K1h）"
                )
                if o_fails:
                    exit_code = 1
                    for msg in o_fails[:30]:
                        print(f"  ❌ {msg}")
                    if len(o_fails) > 30:
                        print(f"  … 另有 {len(o_fails) - 30} 条")
                else:
                    print("  ✅ 首开/补仓/平仓与库内 K 线一致（或在允许容差内）")
            print()
        finally:
            db.close()

    if not skip_db:
        has_ld, missing, bad = verify_symbol_db(path)
        print("── 数据库标的检查（K1d 上市日）──")
        if bad:
            print(f"  ⚠️  {bad} 行缺时间列，已跳过")
        print(f"  有日 K 数据: {has_ld}  无表/无数据: {missing}")
        if missing and has_ld + missing > 0:
            print("  ⚠️  部分品种在库中无 K1d（旧版 verify 对 None 也会“通过”）")
            exit_code = 1 if missing == has_ld + missing else exit_code
        elif has_ld == 0 and (has_ld + missing) > 0:
            exit_code = 1
            print("  ❌ 无法在库中解析任何品种的上市日")
        else:
            print("  ✅ 所检样本均有 listing 数据（或仅跳过空行）")
        print()

    if initial_capital is not None and _is_bt8_export(list(fieldnames) if fieldnames else []):
        total_pnl, exp_final, final_bal, ok = verify_capital_sum(path, initial_capital)
        print("── 资金口径（可选，弱检查）──")
        print(f"  initial={initial_capital:,.2f}  sum(pnl)={total_pnl:+,.2f}  → 初值+∑盈亏={exp_final:,.2f}")
        print(f"  CSV 最后一笔「余额」={final_bal:,.2f}（现金口径，非含券权益）")
        if ok:
            print("  ✅ 与初值+∑盈亏接近（单轨持仓且全部释放后较易成立）")
        else:
            print(
                "  ℹ️  二者常不一致：R24 多笔并行时保证金占用现金，「余额」≠ 权益；"
                "请以回测摘要的 final_capital / 资金曲线为准。"
            )
        print()

    print(
        "── 关于「收益太高」──\n"
        "  公式核对通过只说明 **算术自洽**，不否定策略乐观。建议再做：\n"
        "  • 样本外区间 / walk-forward（rolling_optimizer 已有 OOS）\n"
        "  • 调低杠杆、加上滑点/费率假设压力测试\n"
        "  • 与 signal CSV 交叉核对入场触发时间\n"
        "  • 现货/成交量过滤，排除不可成交价位\n"
        "  • 加 --ohlc 与 PostgreSQL 中 K1h/K5m 逐笔对价"
    )
    return exit_code


def main() -> None:
    p = argparse.ArgumentParser(description="Verify trade CSV (DB + BT8 PnL sanity)")
    p.add_argument("csv_path", help="Path to rolling_*.csv or moonshot_*.csv")
    p.add_argument(
        "--initial-capital",
        type=float,
        default=None,
        help="回测初始资金；若给出则核对 initial + sum(盈亏) ≈ 末余额",
    )
    p.add_argument("--skip-db", action="store_true", help="不连库查 K1d")
    p.add_argument("--skip-pnl", action="store_true", help="不做逐笔盈亏公式核对")
    p.add_argument(
        "--pnl-tol",
        type=float,
        default=1.0,
        help="单笔记盈亏与公式允许差额（美元，默认 1.0）",
    )
    p.add_argument(
        "--ohlc",
        action="store_true",
        help="对照库内 K1h/K5m：开仓价=K1h收盘，平仓价在K5m棒范围内（超时用K1h收盘）",
    )
    p.add_argument(
        "--ohlc-tol",
        type=float,
        default=1e-5,
        help="对价相对容差（默认 1e-5），兼作绝对容差下界",
    )
    p.add_argument(
        "--timing",
        action="store_true",
        help="与 --ohlc 联用：打印 OHLC 段耗时及实际 K1h/K5m SQL 执行次数（K5m 缓存命中不计）",
    )
    args = p.parse_args()
    raise SystemExit(
        verify(
            args.csv_path,
            initial_capital=args.initial_capital,
            skip_db=args.skip_db,
            skip_pnl=args.skip_pnl,
            pnl_tol=args.pnl_tol,
            ohlc=args.ohlc,
            ohlc_tol=args.ohlc_tol,
            timing=args.timing,
        )
    )


if __name__ == "__main__":
    main()
