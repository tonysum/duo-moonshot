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
import re
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

# hm1l 卖量暴涨回测：单边名义 × 0.02%（与 hm1l20260409.trading_fee_rate 一致），开+平双边
_SELL_SURGE_MAKER_FEE_RATE = 0.0002


def _sell_surge_maker_round_trip_fees_usd(
    margin: float, leverage: float, entry_price: float, exit_price: float
) -> float:
    """与 hm1l execute_trade/exit_position 一致：建仓名义=margin×lev，平仓名义=exit×(margin×lev/entry)。"""
    if margin <= 0 or leverage <= 0 or entry_price <= 0:
        return 0.0
    entry_notional = margin * leverage
    position_size = entry_notional / entry_price
    exit_notional = exit_price * position_size
    return entry_notional * _SELL_SURGE_MAKER_FEE_RATE + exit_notional * _SELL_SURGE_MAKER_FEE_RATE


def _csv_row_should_skip(line: str) -> bool:
    """跳过 UTF-8 BOM 后以 ``#`` 开头的元数据/注释行（如 ``# moonshot:max_positions=7``）。"""
    s = line.lstrip("\ufeff")
    t = s.lstrip()
    return bool(t) and t.startswith("#")


def _read_max_positions_csv_hint(path: Path) -> int | None:
    """读取 ``export_csv`` 写入的 ``# moonshot:max_positions=N`` 注释；无则返回 None。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            while True:
                line = f.readline()
                if not line:
                    break
                if not _csv_row_should_skip(line):
                    break
                m = re.search(r"max_positions\s*=\s*(\d+)", line, re.I)
                if m:
                    return int(m.group(1))
    except OSError:
        return None
    return None


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


def _trade_csv_kind(fieldnames: list[str] | None) -> str:
    """``bt8``：rolling/r24 导出；``sell_surge``：卖量暴涨类报表（``仓位金额``=保证金）。"""
    if not fieldnames:
        return ""
    f = set(fieldnames)
    if "盈亏金额" in f and "仓位大小" in f and "建仓价格" in f:
        return "bt8"
    if "盈亏金额" in f and "仓位金额" in f and "建仓价" in f and "平仓价" in f:
        return "sell_surge"
    return ""


def _is_verifiable_trade_csv(fieldnames: list[str] | None) -> bool:
    return _trade_csv_kind(fieldnames) in ("bt8", "sell_surge")


def _row_entry_time(row: dict[str, Any], kind: str) -> str:
    if kind == "sell_surge":
        return (row.get("建仓具体时间") or row.get("建仓时间") or "").strip()
    return (row.get("建仓时间") or "").strip()


def _row_exit_time(row: dict[str, Any], kind: str) -> str:
    if kind == "sell_surge":
        return (row.get("平仓具体时间") or row.get("平仓时间") or "").strip()
    return (row.get("平仓时间") or "").strip()


def _row_entry_px_raw(row: dict[str, Any], kind: str) -> Any:
    if kind == "sell_surge":
        v = row.get("建仓价")
        return row.get("建仓价格") if v in (None, "") else v
    return row.get("建仓价格")


def _row_exit_px_raw(row: dict[str, Any], kind: str) -> Any:
    if kind == "sell_surge":
        v = row.get("平仓价")
        return row.get("平仓价格") if v in (None, "") else v
    return row.get("平仓价格")


def _row_add_flag(row: dict[str, Any], kind: str) -> Any:
    if kind == "sell_surge":
        v = row.get("是否有补仓")
        return row.get("是否补仓") if v in (None, "") else v
    return row.get("是否补仓")


def _row_funding(row: dict[str, Any], kind: str) -> Any:
    v = row.get("资金费成本")
    return row.get("资金费") if v in (None, "") else v


def _margin_invest(row: dict[str, Any], kind: str) -> float:
    """开仓保证金（与 ``Account.invest_amount`` / ``close_position_bt8`` 一致）。"""
    if kind == "sell_surge":
        return _f(row.get("仓位金额"))
    return _f(row.get("仓位大小")) * _f(row.get("建仓价格"))


def _pnl_pct_decimal(row: dict[str, Any], kind: str) -> float:
    """原始价格涨跌比例（小数，如 0.16 表示 16%）。sell_surge 表为百分数 + ``%`` 后缀。"""
    v = _f(row.get("盈亏百分比"))
    return v / 100.0 if kind == "sell_surge" else v


def _reported_net_pnl(row: dict[str, Any], kind: str) -> float:
    """与公式核对用的「报表净盈亏」列：sell_surge 优先 ``最终净盈亏``（与 BT8 ``盈亏金额`` 口径对齐）。"""
    if kind == "sell_surge":
        for key in ("最终净盈亏", "真实盈亏金额", "扣交易费后", "盈亏金额"):
            v = row.get(key)
            if v not in (None, ""):
                return _f(v)
        return 0.0
    return _f(row.get("盈亏金额"))


def _f(x: Any) -> float:
    if x is None or x == "":
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "")
    if s.endswith("%"):
        s = s[:-1].strip()
    return float(s)


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


def _floor_5m_open_utc(dt: datetime) -> datetime:
    """将 UTC 时刻落到其所在 5m K 的开盘时间（左端点，与 Binance 一致）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    floored_min = (dt.minute // 5) * 5
    return dt.replace(minute=floored_min, second=0, microsecond=0)


def _same_5m_bucket(a: datetime, b: datetime) -> bool:
    """a、b 是否在同一根 5m K 内（按 UTC 开盘对齐）。

    错误写法「同 UTC 日且 hour、minute 相同」会把 ``23:59:59`` 与 ``23:55`` 开盘的末根 K 判成不同棒。
    """
    return _floor_5m_open_utc(a) == _floor_5m_open_utc(b)


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
    strategy: str = "rolling",
) -> tuple[int, int, int, list[str]]:
    """核对开仓价与 K5m 数据：
      - rolling（默认）：开仓价=建仓时刻首根 K5m close，检查在 [low, high] 区间内
      - raw_surge：开仓价=建仓时刻第二根 K5m open，检查近似等于 open（容差内）
    补仓价=补仓时刻 K5m 上沿可达；平仓价按空头语义与平仓时刻 K5m（超时用 K1h）；
    硬止损一致性：持仓期内任意 K5m high >= 止损价但平仓原因非止损则标记疑似漏记。

    有补仓时**不**核对「建仓价格」与首根 K5m（该列为均价）。

    返回 (通过笔数, 含补仓笔数, 总笔数, 失败说明)。
    """
    fails: list[str] = []
    ok = n_with_add = total = 0
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(row for row in f if not _csv_row_should_skip(row))
        kind = _trade_csv_kind(reader.fieldnames)
        if not _is_verifiable_trade_csv(reader.fieldnames):
            return 0, 0, 0, ["Not a supported trades CSV (need bt8 or sell_surge columns)."]
        for i, row in enumerate(reader, start=2):
            sym = (row.get("交易对") or "").strip()
            if not sym:
                continue
            total += 1
            try:
                entry_t = _parse_dt_utc(_row_entry_time(row, kind))
                exit_t = _parse_dt_utc(_row_exit_time(row, kind))
                entry_px = _f(_row_entry_px_raw(row, kind))
                exit_px = _f(_row_exit_px_raw(row, kind))
            except (TypeError, ValueError) as e:
                fails.append(f"行 {i} {sym}: 时间或价格解析失败 {e}")
                continue

            add_pos = _is_added_position(_row_add_flag(row, kind))
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
                try:
                    c5a = feed.load_5m(sym, add_t - timedelta(hours=1), add_t + timedelta(minutes=10))
                except ValueError as e:
                    fails.append(f"行 {i} {sym}: 交易对名称非法 - {e}")
                    continue
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
            elif strategy == "raw_surge":
                # Entry price = second 5m bar's open at entry_t (r24_raw_surge strategy)
                try:
                    c5e = feed.load_5m(sym, entry_t, entry_t + timedelta(minutes=15))
                except ValueError as e:
                    fails.append(f"行 {i} {sym}: 交易对名称非法 - {e}")
                    continue
                bar_e = c5e[1] if len(c5e) >= 2 else None
                if bar_e is None:
                    fails.append(f"行 {i} {sym}: 建仓时刻无第二根 K5m 数据 {entry_t}")
                    continue
                tol = max(price_abs_tol, price_rel_tol * max(abs(bar_e.open), abs(entry_px), 1e-12))
                if abs(entry_px - bar_e.open) > tol:
                    fails.append(
                        f"行 {i} {sym}: 开仓价 CSV={entry_px} 与第二根K5m open={bar_e.open}"
                        f" 不符 @ {bar_e.open_time}"
                    )
                    continue
            else:
                # Entry price = first 5m bar's close at entry_t (rolling strategy)
                try:
                    c5e = feed.load_5m(sym, entry_t, entry_t + timedelta(minutes=10))
                except ValueError as e:
                    fails.append(f"行 {i} {sym}: 交易对名称非法 - {e}")
                    continue
                bar_e = c5e[0] if c5e else None
                if bar_e is None:
                    fails.append(f"行 {i} {sym}: 建仓时刻无 K5m 数据 {entry_t}")
                    continue
                if not _price_in_bar(entry_px, bar_e, rel_tol=price_rel_tol, abs_tol=price_abs_tol):
                    fails.append(
                        f"行 {i} {sym}: 开仓价 CSV={entry_px} 不在首根K5m区间"
                        f"[{bar_e.low},{bar_e.high}] @ {bar_e.open_time}"
                    )
                    continue

            reason = (row.get("平仓原因") or "").strip()
            if "超时" in reason:
                # RollingRunner：日末 timeout 用「当天 00:00～次日 00:00」全部 1h 末根收盘；
                # 回测结束强平用 ``load_1h(end-1h, end)``。
                try:
                    if exit_t.hour == 23 and exit_t.minute >= 58:
                        day0 = exit_t.replace(hour=0, minute=0, second=0, microsecond=0)
                        c1x = feed.load_1h(sym, day0, day0 + timedelta(days=1))
                    else:
                        c1x = feed.load_1h(sym, exit_t - timedelta(hours=1), exit_t)
                    if not c1x:
                        c1x = feed.load_1h(sym, exit_t - timedelta(hours=6), exit_t + timedelta(hours=2))
                except ValueError as e:
                    fails.append(f"行 {i} {sym}: 交易对名称非法 - {e}")
                    continue
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
                try:
                    c5 = feed.load_5m(sym, exit_t - timedelta(hours=1), exit_t + timedelta(minutes=10))
                except ValueError as e:
                    fails.append(f"行 {i} {sym}: 交易对名称非法 - {e}")
                    continue
                bar = _pick_exit_5m_bar(c5, exit_t)
                if bar is None:
                    fails.append(f"行 {i} {sym}: 平仓时刻无匹配 K5m 棒 {exit_t}（按 UTC 时分对齐）")
                    continue
                ref_px = max(_f(_row_entry_px_raw(row, kind)), exit_px, 1e-12)
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

            # Hard SL consistency: if any 5m bar during holding hit >= sl_price
            # but exit reason is not stop_loss, flag as suspicious
            sl_px_str = row.get("止损价") or ""
            if sl_px_str:
                sl_px = _f(sl_px_str)
                if sl_px > 0 and "止损" not in reason and "超时" not in reason:
                    try:
                        c5_hold = feed.load_5m(sym, entry_t, exit_t + timedelta(minutes=5))
                    except ValueError as e:
                        fails.append(f"行 {i} {sym}: 交易对名称非法 - {e}")
                        continue
                    triggered = [
                        b for b in c5_hold
                        if b.open_time >= entry_t and b.high >= sl_px * (1 - price_rel_tol)
                    ]
                    if triggered:
                        fails.append(
                            f"行 {i} {sym}: 持仓期内 {len(triggered)} 根K5m high≥止损价{sl_px:.6g}"
                            f"（最早 {triggered[0].open_time} high={triggered[0].high}）"
                            f" 但平仓原因={reason!r}，疑似止损漏记"
                        )
                        continue

            ok += 1
    return ok, n_with_add, total, fails


def verify_trade_sanity(
    path: Path,
    max_raw_pnl_pct: float = 1.0,
    min_signal_gap_minutes: int = 0,
) -> tuple[int, int, list[str]]:
    """检查时间合理性与盈亏合理性。

    时间：信号时间 <= 建仓时间 <= 平仓时间，且持仓时长 > 0；
          若 min_signal_gap_minutes > 0，则还检查 (建仓时间 - 信号时间) >= 该阈值，
          用于排查前视偏差（回测两个策略均为 delay_hours=1，建议设 60）。
    盈亏：|盈亏百分比| <= max_raw_pnl_pct（默认 100%，即原始价格涨跌幅不超过 1 倍）；
          空头盈利方向：盈亏百分比 * 杠杆 <= 100%。

    返回 (通过笔数, 总笔数, 失败说明)。
    """
    fails: list[str] = []
    ok = total = 0
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(row for row in f if not _csv_row_should_skip(row))
        kind = _trade_csv_kind(reader.fieldnames)
        if not _is_verifiable_trade_csv(reader.fieldnames):
            return 0, 0, ["Not a supported trades CSV (need bt8 or sell_surge columns)."]
        for i, row in enumerate(reader, start=2):
            sym = (row.get("交易对") or "").strip()
            if not sym:
                continue
            total += 1
            passed = True

            # ── 时间解析 ──────────────────────────────────────────────
            signal_t: datetime | None = None
            entry_t: datetime | None = None
            exit_t: datetime | None = None
            try:
                sig_str = row.get("信号时间") or ""
                if sig_str:
                    signal_t = _parse_dt_utc(sig_str)
                entry_t = _parse_dt_utc(_row_entry_time(row, kind))
                exit_t = _parse_dt_utc(_row_exit_time(row, kind))
            except (TypeError, ValueError) as e:
                fails.append(f"行 {i} {sym}: 时间解析失败 {e}")
                continue

            # 信号时间 <= 建仓时间
            if signal_t is not None and entry_t < signal_t:
                fails.append(
                    f"行 {i} {sym}: 建仓时间 {entry_t} 早于信号时间 {signal_t}"
                )
                passed = False

            # 信号→建仓最小间隔（排查前视偏差）
            if signal_t is not None and min_signal_gap_minutes > 0:
                gap_min = (entry_t - signal_t).total_seconds() / 60.0
                if gap_min < min_signal_gap_minutes:
                    fails.append(
                        f"行 {i} {sym}: 信号→建仓间隔 {gap_min:.1f}min"
                        f" < 阈值 {min_signal_gap_minutes}min（疑似前视偏差，"
                        f"signal={signal_t} entry={entry_t}）"
                    )
                    passed = False

            # 建仓时间 < 平仓时间
            if exit_t <= entry_t:
                fails.append(
                    f"行 {i} {sym}: 平仓时间 {exit_t} 不晚于建仓时间 {entry_t}（持仓时长 <= 0）"
                )
                passed = False

            # ── 盈亏合理性 ────────────────────────────────────────────
            try:
                signed_pct = _pnl_pct_decimal(row, kind)
                lev = _f(row.get("杠杆倍数")) or 1.0
            except (TypeError, ValueError):
                signed_pct = lev = 0.0

            raw_pct = abs(signed_pct)

            if raw_pct > max_raw_pnl_pct:
                fails.append(
                    f"行 {i} {sym}: |盈亏百分比|={raw_pct:.4f} 超过阈值 {max_raw_pnl_pct:.0%}（原始价格异常波动）"
                )
                passed = False

            # 空头盈利方向：价格下跌 → 盈亏百分比 > 0；理论最大盈利 = 价格跌到 0 = 100% notional
            # 亏损方向无上限（止损决定亏多少），不做此限制
            if signed_pct > 0 and lev > 0 and signed_pct * lev > 1.0:
                fails.append(
                    f"行 {i} {sym}: 盈利方向 盈亏% × 杠杆={signed_pct * lev:.4f} > 100%"
                    f"（空头价格不可能跌破 0，疑似数据异常）"
                )
                passed = False

            if passed:
                ok += 1
    return ok, total, fails


def verify_position_capacity(
    path: Path,
    *,
    max_positions: int | None = None,
    initial_capital: float | None = None,
) -> tuple[dict, list[str]]:
    """检查同时持仓数量与资金容量合理性。

    用扫描线算法遍历所有 [entry_time, exit_time] 区间，找出任意时刻的并发持仓数
    和总占用保证金（BT8：仓位大小×建仓价/杠杆；sell_surge：``仓位金额`` 列即保证金），报告峰值及超限情况。

    检查项：
    - 同 symbol 并发持仓（同一 symbol 尚未平仓时又开新仓）
    - 并发持仓数 > max_positions（若指定）
    - 总占用保证金 > initial_capital（若指定）

    返回 (stats, 失败说明列表)。
    """
    fails: list[str] = []

    trades: list[tuple] = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(row for row in f if not _csv_row_should_skip(row))
        kind = _trade_csv_kind(reader.fieldnames)
        if not _is_verifiable_trade_csv(reader.fieldnames):
            return {}, ["Not a supported trades CSV (need bt8 or sell_surge columns)."]
        for i, row in enumerate(reader, start=2):
            sym = (row.get("交易对") or "").strip()
            if not sym:
                continue
            try:
                entry_t = _parse_dt_utc(_row_entry_time(row, kind))
                exit_t = _parse_dt_utc(_row_exit_time(row, kind))
                lev = _f(row.get("杠杆倍数")) or 1.0
                if kind == "sell_surge":
                    invest = _f(row.get("仓位金额"))
                else:
                    size = _f(row.get("仓位大小"))
                    entry_px = _f(row.get("建仓价格"))
                    invest = size * entry_px / lev if lev > 0 and entry_px > 0 else 0.0
            except (TypeError, ValueError):
                continue
            trades.append((entry_t, exit_t, sym, invest, i))

    if not trades:
        return {}, ["无有效交易数据"]

    # 扫描线事件：同一时刻先出后入，避免平仓重新开同 symbol 被误报
    events: list[tuple] = []
    for entry_t, exit_t, sym, invest, row_i in trades:
        events.append((entry_t, 1, invest, sym, row_i))   # 1=入
        events.append((exit_t,  0, invest, sym, row_i))   # 0=出（先处理）
    events.sort(key=lambda e: (e[0], e[1]))  # 同时刻：出(0)先于入(1)

    open_pos: dict[str, tuple[float, int]] = {}  # symbol -> (invest, row_i)
    max_concurrent = 0
    max_concurrent_time: datetime | None = None
    max_invest_val = 0.0
    max_invest_time: datetime | None = None

    for t, etype, invest, sym, row_i in events:
        if etype == 1:  # entry
            if sym in open_pos:
                fails.append(
                    f"行 {row_i} {sym}: {t} 开仓时该 symbol 仍有未平仓位（同 symbol 并发持仓）"
                )
            open_pos[sym] = (invest, row_i)
            n = len(open_pos)
            total_inv = sum(v[0] for v in open_pos.values())
            if n > max_concurrent:
                max_concurrent = n
                max_concurrent_time = t
            if total_inv > max_invest_val:
                max_invest_val = total_inv
                max_invest_time = t
            if max_positions is not None and n > max_positions:
                fails.append(
                    f"{t}: 并发持仓 {n} 超过 --max-positions={max_positions}"
                    f"（{', '.join(open_pos.keys())}）"
                )
            if initial_capital is not None and total_inv > initial_capital * 1.01:
                fails.append(
                    f"{t}: 总保证金占用 {total_inv:.2f} 超过初始资金 {initial_capital:.2f}"
                )
        else:  # exit
            open_pos.pop(sym, None)

    return {
        "total_trades": len(trades),
        "max_concurrent": max_concurrent,
        "max_concurrent_time": max_concurrent_time,
        "max_invest": max_invest_val,
        "max_invest_time": max_invest_time,
    }, fails


def _expected_profit_amount(
    invest_margin: float,
    entry_price: float,
    exit_price: float,
    leverage: float,
    funding_fee_cost: float,
    *,
    fees_usd: float | None = None,
) -> float:
    """与 ``close_position_bt8`` 一致：``invest_margin`` 为开仓保证金（非名义）。

    ``fees_usd`` 若给出（如 CSV ``交易手续费合计``），则替代 ``invest_margin * _FEE_RATE_RT``。
    """
    if entry_price <= 0 or invest_margin <= 0:
        return 0.0
    actual_pct = (entry_price - exit_price) / entry_price * 100.0
    leveraged_pct = actual_pct * leverage
    gross = invest_margin * leveraged_pct / 100.0
    fees = invest_margin * _FEE_RATE_RT if fees_usd is None else fees_usd
    return gross - funding_fee_cost - fees


def verify_bt8_pnl(path: Path, tol_abs: float = 1.0) -> tuple[int, int, list[str]]:
    """逐笔核对盈亏金额与 BT8 公式。返回 (匹配行数, 总行数, 失败信息列表)。"""
    fails: list[str] = []
    ok = total = 0
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(row for row in f if not _csv_row_should_skip(row))
        kind = _trade_csv_kind(reader.fieldnames)
        if not _is_verifiable_trade_csv(reader.fieldnames):
            return 0, 0, ["Not a supported trades CSV (bt8 or sell_surge columns)."]
        for i, row in enumerate(reader, start=2):
            sym = (row.get("交易对") or "").strip()
            if not sym:
                continue
            total += 1
            try:
                invest = _margin_invest(row, kind)
                entry = _f(_row_entry_px_raw(row, kind))
                exit_px = _f(_row_exit_px_raw(row, kind))
                lev = _f(row.get("杠杆倍数")) or 1.0
                reported = _reported_net_pnl(row, kind)
                funding = _f(_row_funding(row, kind))
                pct_col = _pnl_pct_decimal(row, kind)
                fee_csv = _f(row.get("交易手续费合计"))
            except (TypeError, ValueError) as e:
                fails.append(f"行 {i} {sym}: 数值解析失败 {e}")
                continue

            if kind == "sell_surge":
                fees_arg = (
                    fee_csv
                    if fee_csv > 0
                    else _sell_surge_maker_round_trip_fees_usd(invest, lev, entry, exit_px)
                )
            else:
                fees_arg = None
            expected = _expected_profit_amount(
                invest,
                entry,
                exit_px,
                lev,
                funding,
                fees_usd=fees_arg,
            )
            row_tol = max(tol_abs, 1e-4 * max(abs(expected), abs(reported), 1.0))
            if abs(reported - expected) > row_tol:
                fails.append(
                    f"行 {i} {sym}: 盈亏金额 报表={reported:.2f} 按公式≈{expected:.2f} "
                    f"(margin≈{invest:.2f} lev={lev:g})"
                )
                continue

            raw_pct = (entry - exit_px) / entry if entry else 0.0
            if (
                kind != "sell_surge"
                and pct_col != 0.0
                and abs(raw_pct - pct_col) > 2e-4
                and abs(raw_pct - pct_col) / max(abs(pct_col), 1e-9) > 0.02
            ):
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
            reader = csv.DictReader(row for row in f if not _csv_row_should_skip(row))
            for row in reader:
                symbol = row.get("Symbol") or row.get("交易对")
                if not symbol:
                    continue
                t_ref = (
                    row.get("建仓时间")
                    or row.get("建仓具体时间")
                    or row.get("信号时间")
                    or row.get("信号日期")
                    or row.get("Date")
                )
                if not t_ref:
                    bad += 1
                    continue
                try:
                    ld = feed.load_listing_date(symbol.strip())
                except ValueError:
                    # CSV 里偶尔会混入备注/脏数据（例如中文句子），跳过而不是让整次 verify 崩溃。
                    bad += 1
                    continue
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
        reader = csv.DictReader(row for row in f if not _csv_row_should_skip(row))
        kind = _trade_csv_kind(reader.fieldnames)
        for row in reader:
            total_pnl += _reported_net_pnl(row, kind)
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
    strategy: str = "rolling",
    min_signal_gap: int = 0,
    max_positions: int | None = None,
):
    path = Path(csv_path)
    if not path.exists():
        print(f"❌ File not found: {path}")
        return 1

    print(f"📋 Verifying {path.name}...\n")

    hint_mp = _read_max_positions_csv_hint(path)
    eff_max_pos = max_positions if max_positions is not None else hint_mp
    if max_positions is not None and hint_mp is not None and hint_mp != max_positions:
        print(
            f"⚠️  CSV 注释标明 max_positions={hint_mp}，与 --max-positions={max_positions} 不一致。"
            f"并发检查按 CLI（{max_positions}）。若误报请改用 --max-positions {hint_mp}，"
            f"或在 JSON 中设 max_positions={max_positions} 后重新回测导出。\n"
        )
    elif max_positions is None and hint_mp is not None:
        print(f"ℹ️  未指定 --max-positions，使用 CSV 注释 max_positions={hint_mp} 做并发检查。\n")

    with open(path, encoding="utf-8-sig") as f:
        f.readline()
        f.seek(0)
        reader = csv.DictReader(row for row in f if not _csv_row_should_skip(row))
        fieldnames = reader.fieldnames

    exit_code = 0

    if _is_verifiable_trade_csv(list(fieldnames) if fieldnames else []):
        ok_s, total_s, s_fails = verify_trade_sanity(path, min_signal_gap_minutes=min_signal_gap)
        print("── 时间与盈亏合理性检查 ──")
        if total_s == 0:
            print(f"  ⚠️  {s_fails[0] if s_fails else '无数据行'}")
            exit_code = 1
        else:
            print(f"  通过 {ok_s}/{total_s} 笔")
            if s_fails:
                exit_code = 1
                for msg in s_fails:
                    print(f"  ❌ {msg}")
            else:
                print("  ✅ 时间顺序正确，盈亏百分比在合理范围内")
        print()

    if _is_verifiable_trade_csv(list(fieldnames) if fieldnames else []):
        cap_stats, cap_fails = verify_position_capacity(
            path, max_positions=eff_max_pos, initial_capital=initial_capital
        )
        print("── 持仓容量合理性检查 ──")
        if not cap_stats:
            print(f"  ⚠️  {cap_fails[0] if cap_fails else '无数据'}")
            exit_code = 1
        else:
            mc = cap_stats["max_concurrent"]
            mc_t = cap_stats["max_concurrent_time"]
            mi = cap_stats["max_invest"]
            mi_t = cap_stats["max_invest_time"]
            limit_str = f"（上限 {eff_max_pos}）" if eff_max_pos else ""
            cap_str = f"（初始资金 {initial_capital:.0f}）" if initial_capital else ""
            print(f"  最大并发持仓: {mc} 笔{limit_str} @ {mc_t}")
            print(f"  最大总保证金: {mi:.2f}{cap_str} @ {mi_t}")
            if cap_fails:
                exit_code = 1
                for msg in cap_fails:
                    print(f"  ❌ {msg}")
            else:
                print("  ✅ 无同 symbol 并发持仓" + ("，并发数与资金均在限额内" if eff_max_pos or initial_capital else ""))
        print()

    if not skip_pnl and _is_verifiable_trade_csv(list(fieldnames) if fieldnames else []):
        ok, total, fails = verify_bt8_pnl(path, tol_abs=pnl_tol)
        print("── 盈亏公式核对（BT8 / sell_surge，与 account.close_position_bt8 一致）──")
        if total == 0:
            print(f"  ⚠️  {fails[0] if fails else '无数据行'}")
            exit_code = 1
        else:
            print(f"  匹配 {ok}/{total} 笔（容差 ${pnl_tol:g}）")
            if fails:
                exit_code = 1
                for msg in fails:
                    print(f"  ❌ {msg}")
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
                path, feed, price_rel_tol=ohlc_tol, price_abs_tol=max(1e-12, ohlc_tol * 1e-8),
                strategy=strategy,
            )
            ohlc_s = time.perf_counter() - t0
            entry_desc = "第二根K5m open" if strategy == "raw_surge" else "首根K5m区间"
            print(f"── OHLC 与库内 K 线对照（开仓：{entry_desc}；补仓：K5m上沿；平仓：K5m语义；硬止损一致性）──")
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
                    f"  通过 {ok_o}/{total_o} 笔（无补仓 {n_plain} 笔：核对{entry_desc}开仓价 + 平仓 + 硬止损一致性；"
                    f"含补仓 {n_add} 笔：另核对补仓 K5m + 平仓，首仓均价不对照 K5m）"
                )
                if o_fails:
                    exit_code = 1
                    for msg in o_fails:
                        print(f"  ❌ {msg}")
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

    if initial_capital is not None and _is_verifiable_trade_csv(list(fieldnames) if fieldnames else []):
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
    p.add_argument(
        "--strategy",
        choices=["rolling", "raw_surge"],
        default="rolling",
        help="策略类型，影响开仓价核对逻辑：rolling=首根K5m close区间；raw_surge=第二根K5m open（默认 rolling）",
    )
    p.add_argument(
        "--min-signal-gap",
        type=int,
        default=0,
        metavar="MINUTES",
        help="信号→建仓最小间隔（分钟）；两种策略均使用 delay_hours=1，建议设 60 排查前视偏差（默认 0=不检查）",
    )
    p.add_argument(
        "--max-positions",
        type=int,
        default=None,
        metavar="N",
        help="最大并发持仓数上限；省略时若 CSV 首行有 # moonshot:max_positions=N 则用 N，否则不检查并发数",
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
            strategy=args.strategy,
            min_signal_gap=args.min_signal_gap,
            max_positions=args.max_positions,
        )
    )


if __name__ == "__main__":
    main()
