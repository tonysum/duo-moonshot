"""Paper closed trades → CSV aligned with moonshot/runner.py and rolling_runner export_csv."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Literal

from moonshot.models import derive_exit_reason

Variant = Literal["daily", "rolling"]


def _fmt_dt(iso_val: Any) -> str:
    if not iso_val:
        return ""
    s = str(iso_val).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(iso_val)[:19]


def headers(variant: Variant) -> list[str]:
    surge_col = "24h涨幅" if variant == "rolling" else "涨幅"
    return [
        "信号时间",
        "信号价格",
        "建仓时间",
        "交易对",
        "建仓价格",
        surge_col,
        "卖量倍数",
        "昨日小时均卖",
        "仓位大小",
        "杠杆倍数",
        "止盈价(初始)",
        "止盈价(实际)",
        "止损价",
        "开仓理由",
        "平仓时间",
        "平仓价格",
        "平仓原因",
        "盈亏金额",
        "余额",
        "盈亏百分比",
        "持仓小时数",
        "补仓价格",
        "补仓时间",
        "平均建仓价格",
        "是否补仓",
        "建仓多空比",
        "平仓多空比",
        "多空比变化",
        "资金费成本",
    ]


def _holding_hours_fallback(t: dict[str, Any]) -> Any:
    h = t.get("holding_hours")
    if h is not None:
        return h
    et, xt = t.get("entry_time"), t.get("exit_time")
    if not et or not xt:
        return ""
    try:
        a = datetime.fromisoformat(str(et).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(xt).replace("Z", "+00:00"))
        return int((b - a).total_seconds() / 3600)
    except (TypeError, ValueError):
        return ""


def trade_row(t: dict[str, Any], variant: Variant) -> list[Any]:
    """One paper trade dict (model_dump + close fields) → CSV row，与 backtest export_csv 列一致。"""
    entry_px = float(t.get("entry_price") or 0)
    exit_px = t.get("exit_price")
    exit_px_f = float(exit_px) if exit_px is not None else None
    raw_pnl_pct = (entry_px - exit_px_f) / entry_px if entry_px > 0 and exit_px_f is not None else 0.0

    tp_init = t.get("tp_initial_price")
    if tp_init is None:
        tp_init = t.get("tp_price")
    tp_act = t.get("tp_price")

    has_add = bool(t.get("has_added_position"))
    avg_entry = round(entry_px, 8) if has_add else ""

    exit_reason = derive_exit_reason(t.get("result"))

    sig_px = t.get("signal_price")
    if sig_px is None:
        sig_px = entry_px

    entry_ts = _fmt_dt(t.get("entry_time"))

    ff = t.get("funding_fee")
    if ff is None:
        fund_cell: Any = ""
    else:
        try:
            fv = float(ff)
            fund_cell = "" if fv == 0 else round(fv, 2)
        except (TypeError, ValueError):
            fund_cell = ""

    # 卖量相关字段（与回测 CSV 对齐）
    sell_surge_ratio = t.get("sell_surge_ratio")
    yesterday_avg_hour_sell_volume = t.get("yesterday_avg_hour_sell_volume")

    return [
        entry_ts,
        round(float(sig_px), 8) if sig_px is not None else "",
        entry_ts,
        t.get("symbol", ""),
        entry_px,
        t.get("surge_pct", ""),
        round(float(sell_surge_ratio), 4) if sell_surge_ratio is not None else "",
        round(float(yesterday_avg_hour_sell_volume), 2) if yesterday_avg_hour_sell_volume is not None else "",
        t.get("position_size", ""),
        t.get("leverage", ""),
        round(float(tp_init), 8) if tp_init is not None else "",
        round(float(tp_act), 8) if tp_act is not None else "",
        round(float(t.get("sl_price") or 0), 8) if t.get("sl_price") is not None else "",
        t.get("entry_reason") or "",
        _fmt_dt(t.get("exit_time")),
        round(float(exit_px), 8) if exit_px is not None else "",
        exit_reason,
        round(float(t.get("net_pnl") or 0), 2),
        round(float(t.get("capital_after") or 0), 2) if t.get("capital_after") is not None else "",
        round(raw_pnl_pct, 6),
        _holding_hours_fallback(t),
        round(float(t["add_price"]), 8) if t.get("add_price") is not None else "",
        _fmt_dt(t.get("add_time")) if t.get("add_time") else "",
        avg_entry,
        has_add,
        round(float(t["entry_account_ratio"]), 4) if t.get("entry_account_ratio") is not None else "",
        round(float(t["exit_account_ratio"]), 4) if t.get("exit_account_ratio") is not None else "",
        round(float(t["account_ratio_change"]), 4) if t.get("account_ratio_change") is not None else "",
        fund_cell,
    ]


def build_trades_csv(
    trades_desc: list[dict[str, Any]],
    variant: Variant,
    *,
    include_summary: bool = True,
    summary_lines: list[tuple[str, Any]] | None = None,
) -> bytes:
    """trades_desc: newest-first from DB; export chronological (oldest first). UTF-8 BOM for Excel."""
    rows = list(reversed(trades_desc))
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(headers(variant))
    for t in rows:
        writer.writerow(trade_row(t, variant))

    if include_summary and summary_lines:
        writer.writerow([])
        writer.writerow(["# Summary"])
        for k, v in summary_lines:
            writer.writerow([k, v])

    raw = buf.getvalue().encode("utf-8-sig")
    return raw
