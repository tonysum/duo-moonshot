#!/usr/bin/env python3
"""
回测核查程序 (jchc.py)
用5分钟K线数据验证回测报告的真实性

检查内容：
1. 建仓价格 - 是否落在建仓所在小时「第一根5分钟K线」的 high~low 区间内（含边界）
2. 平仓价格 - 先定位平仓时刻所在的那根5分钟K线，再判断平仓价是否落在该根 high~low 区间内（含边界）
3. 硬止损一致性 - 持仓区间内任意5m若 high >= 理论+18%止损价，但平仓原因不是 stop_loss（疑似该止损未记为止损，需对照 hm1l 优先级）
"""

import os
import sys
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from dotenv import load_dotenv
from moonshot.db import get_postgres_db


# 加载环境变量
load_dotenv()

# 止损阈值（做空策略，与 hm1l sl_price = entry * (1 - stop_loss_pct/100)，stop_loss_pct=-18 一致）
STOP_LOSS_PCT = 18.0  # +18%触发止损
STOP_LOSS_PRICE_MULT = 1.0 + STOP_LOSS_PCT / 100.0

# 5 分钟毫秒（币安 K 线：open_time 为周期起点，区间 [open_time, open_time+5m)）
BAR_MS_5M = 5 * 60 * 1000


class BacktestChecker:
    """回测核查器"""

    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self.df = None
        self.issues = []

    def load_csv(self):
        """加载CSV报告"""
        try:
            self.df = pd.read_csv(self.csv_path)
            # sell_surge_backtest_report 等导出使用「建仓具体时间/建仓价」；旧表用「建仓时间/建仓价格」
            col_map = {}
            if '建仓时间' not in self.df.columns and '建仓具体时间' in self.df.columns:
                col_map['建仓具体时间'] = '建仓时间'
            if '建仓价格' not in self.df.columns and '建仓价' in self.df.columns:
                col_map['建仓价'] = '建仓价格'
            if '平仓时间' not in self.df.columns and '平仓具体时间' in self.df.columns:
                col_map['平仓具体时间'] = '平仓时间'
            if '平仓价格' not in self.df.columns and '平仓价' in self.df.columns:
                col_map['平仓价'] = '平仓价格'
            if col_map:
                self.df = self.df.rename(columns=col_map)
            print(f"✅ 加载CSV: {len(self.df)} 笔交易")
            return True
        except Exception as e:
            print(f"❌ 加载CSV失败: {e}")
            return False

    def get_5m_kline_first_of_hour(self, symbol: str, hour_datetime: str) -> Optional[pd.Series]:
        """获取指定小时的第一根5分钟K线"""
        try:
            db = get_postgres_db().connect()
            conn = db.conn

            hour_dt = pd.to_datetime(hour_datetime)
            hour_start_ts = int(hour_dt.timestamp() * 1000)
            hour_end_ts = hour_start_ts + 60 * 60 * 1000

            table_name = f'K5m{symbol}'
            query = f"""
                SELECT *
                FROM "{table_name}"
                WHERE open_time >= %s AND open_time < %s
                ORDER BY open_time ASC
                LIMIT 1
            """

            cursor = conn.cursor()
            cursor.execute(query, (hour_start_ts, hour_end_ts))
            rows = cursor.fetchall()
            cursor.close()
            db.close()

            if rows:
                # 使用前5个列：open_time, open, high, low, close
                # 列索引：0=open_time, 1=open, 2=high, 3=low, 4=close
                row = rows[0]
                filtered_row = (row[0], row[1], row[2], row[3], row[4])
                columns = ['open_time', 'open', 'high', 'low', 'close']
                df = pd.DataFrame([filtered_row], columns=columns)
                return df.iloc[0]
            return None

        except Exception as e:
            if 'cursor' in locals():
                cursor.close()
            if 'db' in locals():
                db.close()
            return None

    def get_5m_klines_in_hour(self, symbol: str, hour_datetime: str) -> pd.DataFrame:
        """获取指定小时的所有5分钟K线"""
        try:
            db = get_postgres_db().connect()
            conn = db.conn

            hour_dt = pd.to_datetime(hour_datetime)
            hour_start_ts = int(hour_dt.timestamp() * 1000)
            hour_end_ts = hour_start_ts + 60 * 60 * 1000

            table_name = f'K5m{symbol}'
            query = f"""
                SELECT *
                FROM "{table_name}"
                WHERE open_time >= %s AND open_time < %s
                ORDER BY open_time ASC
            """

            cursor = conn.cursor()
            cursor.execute(query, (hour_start_ts, hour_end_ts))
            rows = cursor.fetchall()
            cursor.close()
            db.close()

            if rows:
                # 使用前5个列：open_time, open, high, low, close
                # 列索引：0=open_time, 1=open, 2=high, 3=low, 4=close
                filtered_rows = [(row[0], row[1], row[2], row[3], row[4]) for row in rows]
                columns = ['open_time', 'open', 'high', 'low', 'close']
                df = pd.DataFrame(filtered_rows, columns=columns)
                return df
            return pd.DataFrame()

        except Exception as e:
            if 'cursor' in locals():
                cursor.close()
            if 'db' in locals():
                db.close()
            return pd.DataFrame()

    def get_5m_kline_containing_timestamp(self, symbol: str, dt) -> Optional[pd.Series]:
        """找到覆盖给定时刻的那根5分钟K：open_time <= ts_ms < open_time + 5m。"""
        try:
            ts_ms = int(pd.to_datetime(dt).timestamp() * 1000)
            db = get_postgres_db().connect()
            conn = db.conn
            table_name = f'K5m{symbol}'
            query = f"""
                SELECT *
                FROM "{table_name}"
                WHERE open_time <= %s
                ORDER BY open_time DESC
                LIMIT 1
            """
            cursor = conn.cursor()
            cursor.execute(query, (ts_ms,))
            rows = cursor.fetchall()
            cursor.close()
            db.close()
            if not rows:
                return None
            # 使用前5个列：open_time, open, high, low, close
            # 列索引：0=open_time, 1=open, 2=high, 3=low, 4=close
            db_row = rows[0]
            filtered_row = (db_row[0], db_row[1], db_row[2], db_row[3], db_row[4])
            columns = ['open_time', 'open', 'high', 'low', 'close']
            df = pd.DataFrame([filtered_row], columns=columns)
            row = df.iloc[0]
            bar_open = int(row['open_time'])
            if ts_ms >= bar_open + BAR_MS_5M:
                return None
            return row
        except Exception:
            if 'cursor' in locals():
                cursor.close()
            if 'db' in locals():
                db.close()
            return None

    @staticmethod
    def _price_in_hl(price: float, low: float, high: float) -> bool:
        """成交价是否合理：落在该根K的 low~high 之间（含边界，避免浮点毛刺用极小裕量）。"""
        eps = max(1e-12, abs(price) * 1e-10)
        return (low - eps) <= price <= (high + eps)

    def _entry_baseline_price(self, trade: pd.Series) -> float:
        """做空参考建仓价：有补仓且有效时用补仓后平均价，否则用建仓价格。"""
        px = float(trade['建仓价格'])
        if '是否有补仓' in trade.index and str(trade.get('是否有补仓', '')).strip() == '是':
            if '补仓后平均价' in trade.index:
                ap = trade['补仓后平均价']
                if pd.notna(ap) and str(ap).strip() not in ('', 'nan'):
                    try:
                        px = float(str(ap).replace(',', ''))
                    except ValueError:
                        pass
        return px

    def _max_high_in_hold(self, symbol: str, t0_ms: int, t1_ms: int, conn) -> Optional[float]:
        """持仓时间区间内 5m 最高价；无表或无数据返回 None。"""
        try:
            table_name = f'K5m{symbol}'
            q = f'SELECT * FROM "{table_name}" WHERE open_time >= %s AND open_time <= %s'
            cursor = conn.cursor()
            cursor.execute(q, (t0_ms, t1_ms))
            rows = cursor.fetchall()
            cursor.close()
            if not rows:
                return None
            # 提取 high 值（列索引2）
            highs = []
            for row in rows:
                if row[2] is not None:
                    highs.append(float(row[2]))
            if not highs:
                return None
            return max(highs)
        except Exception:
            return None

    def _first_bar_touching_high(self, symbol: str, t0_ms: int, t1_ms: int,
                                  min_high: float, conn) -> Optional[int]:
        """第一根 open_time（毫秒）满足 high >= min_high。"""
        try:
            table_name = f'K5m{symbol}'
            q = (
                f'SELECT open_time FROM "{table_name}" '
                f'WHERE open_time >= %s AND open_time <= %s AND high >= %s '
                f'ORDER BY open_time ASC LIMIT 1'
            )
            cursor = conn.cursor()
            cursor.execute(q, (t0_ms, t1_ms, min_high))
            row = cursor.fetchone()
            cursor.close()
            return int(row[0]) if row else None
        except Exception:
            return None

    def check_missed_stop_loss_label(self, trade: pd.Series, conn) -> Optional[Dict]:
        """持仓全程 5m 曾触及理论硬止损价，但报告平仓原因不是 stop_loss。

        仅作数据侧告警：回测可能因 flatten/提前止损优先级、小时级判定与 5m 极值差异等未标 stop_loss。
        """
        reason = str(trade['平仓原因'])
        if reason == 'stop_loss':
            return None

        entry_dt = pd.to_datetime(trade['建仓时间'])
        exit_dt = pd.to_datetime(trade['平仓时间'])
        t0_ms = int(entry_dt.timestamp() * 1000)
        t1_ms = int(exit_dt.timestamp() * 1000)

        baseline = self._entry_baseline_price(trade)
        sl_price = baseline * STOP_LOSS_PRICE_MULT
        symbol = trade['交易对']

        max_high = self._max_high_in_hold(symbol, t0_ms, t1_ms, conn)
        if max_high is None:
            return None

        if max_high < sl_price - 1e-12:
            return None

        first_ms = self._first_bar_touching_high(symbol, t0_ms, t1_ms, sl_price, conn)
        return {
            'type': 'missed_sl_label',
            'symbol': symbol,
            'exit_reason': reason,
            'entry_time': trade['建仓时间'],
            'exit_time': trade['平仓时间'],
            'baseline_entry': baseline,
            'sl_price_18pct': sl_price,
            'max_high_5m': max_high,
            'first_touch_open_time_ms': first_ms,
            'note': '全程5m最高价已达+18%止损线，但平仓原因非 stop_loss',
        }

    def check_entry_price(self, trade: pd.Series) -> Optional[Dict]:
        """检查建仓价格

        逻辑：
        - 建仓时间显示为XX:05:00或XX:00:00（与原先一致，用于定位「该小时第一根5m」）
        - 建仓价格须落在该小时第一根5分钟K线的 low ~ high 之间（含边界）
        """
        symbol = trade['交易对']
        entry_datetime = trade['建仓时间']
        try:
            entry_price = float(str(trade['建仓价格']).replace(',', ''))
        except (TypeError, ValueError):
            return {
                'type': 'entry',
                'symbol': symbol,
                'datetime': entry_datetime,
                'price': trade['建仓价格'],
                'issue': '建仓价格无法解析为数值',
            }

        entry_dt = pd.to_datetime(entry_datetime)
        if entry_dt.minute == 5:
            hour_start = entry_dt - timedelta(minutes=5)
        else:
            hour_start = entry_dt.replace(minute=0, second=0, microsecond=0)

        kline = self.get_5m_kline_first_of_hour(symbol, hour_start.strftime('%Y-%m-%d %H:%M:%S'))

        if kline is None:
            return {
                'type': 'entry',
                'symbol': symbol,
                'datetime': entry_datetime,
                'price': entry_price,
                'issue': '无5分钟K线数据',
            }

        lo, hi = float(kline['low']), float(kline['high'])
        if self._price_in_hl(entry_price, lo, hi):
            return None

        return {
            'type': 'entry',
            'symbol': symbol,
            'datetime': entry_datetime,
            'price': entry_price,
            'kline_time': pd.to_datetime(int(kline['open_time']), unit='ms').strftime('%Y-%m-%d %H:%M:%S'),
            'kline_low': lo,
            'kline_high': hi,
            'issue': f'建仓价格不在首根5m的[low,high]内（low={lo:.8g}, high={hi:.8g}）',
        }

    def check_exit_price(self, trade: pd.Series) -> Optional[Dict]:
        """检查平仓价格

        逻辑：根据平仓时间定位「包含该时刻」的那根5分钟K线，
        平仓价须落在该根K的 low ~ high 之间（含边界）。
        """
        symbol = trade['交易对']
        exit_datetime = trade['平仓时间']
        exit_reason = trade['平仓原因']
        try:
            exit_price = float(str(trade['平仓价格']).replace(',', ''))
        except (TypeError, ValueError):
            return {
                'type': 'exit',
                'symbol': symbol,
                'datetime': exit_datetime,
                'price': trade['平仓价格'],
                'reason': exit_reason,
                'issue': '平仓价格无法解析为数值',
            }

        kline = self.get_5m_kline_containing_timestamp(symbol, exit_datetime)

        if kline is None:
            return {
                'type': 'exit',
                'symbol': symbol,
                'datetime': exit_datetime,
                'price': exit_price,
                'reason': exit_reason,
                'issue': '无覆盖平仓时刻的5分钟K线数据',
            }

        lo, hi = float(kline['low']), float(kline['high'])
        if self._price_in_hl(exit_price, lo, hi):
            return None

        return {
            'type': 'exit',
            'symbol': symbol,
            'datetime': exit_datetime,
            'price': exit_price,
            'reason': exit_reason,
            'kline_time': pd.to_datetime(int(kline['open_time']), unit='ms').strftime('%Y-%m-%d %H:%M:%S'),
            'kline_low': lo,
            'kline_high': hi,
            'issue': (
                f'平仓价不在该根5m的[low,high]内（low={lo:.8g}, high={hi:.8g}）'
            ),
        }

    def run_check(self):
        """执行检查"""
        if not self.load_csv():
            return False

        print(f"\n{'='*80}")
        print("🔍 开始检查...")
        print(f"{'='*80}\n")

        entry_issues = []
        exit_issues = []
        missed_sl_issues: List[Dict] = []

        db = get_postgres_db().connect()
        conn_hold = db.conn
        try:
            for idx, trade in self.df.iterrows():
                # 显示进度
                if (idx + 1) % 50 == 0:
                    print(f"  [{idx+1}/{len(self.df)}] 已检查 {idx+1} 笔交易...")

                # 检查建仓
                issue = self.check_entry_price(trade)
                if issue:
                    entry_issues.append(issue)

                # 检查平仓
                issue = self.check_exit_price(trade)
                if issue:
                    exit_issues.append(issue)

                msl = self.check_missed_stop_loss_label(trade, conn_hold)
                if msl:
                    missed_sl_issues.append(msl)
        finally:
            db.close()

        print(f"\n✅ 检查完成: {len(self.df)} 笔交易\n")

        # 输出结果
        self.print_results(entry_issues, exit_issues, missed_sl_issues)

        return True

    def print_results(self, entry_issues: List[Dict], exit_issues: List[Dict],
                      missed_sl_issues: Optional[List[Dict]] = None):
        """输出检查结果"""
        if missed_sl_issues is None:
            missed_sl_issues = []
        print(f"{'='*80}")
        print("📊 检查结果")
        print(f"{'='*80}\n")

        total_trades = len(self.df)

        # 统计各类平仓原因
        exit_reasons = self.df['平仓原因'].value_counts()

        # 建仓价格检查结果
        print(f"1️⃣  建仓价格检查")
        print(f"   总交易数: {total_trades}")
        print(f"   问题数: {len(entry_issues)}")

        if entry_issues:
            print(f"\n   ⚠️  发现 {len(entry_issues)} 笔建仓价格问题：\n")
            print(f"   {'交易对':<15} {'建仓时间':<22} {'建仓价格':<14} {'首5m low':<12} {'首5m high':<12} {'问题':<40}")
            print(f"   {'-'*120}")

            for issue in entry_issues[:20]:  # 只显示前20个
                kl = issue.get('kline_low', None)
                kh = issue.get('kline_high', None)
                if isinstance(kl, (int, float)) and isinstance(kh, (int, float)):
                    print(f"   {issue['symbol']:<15} {str(issue['datetime']):<22} "
                          f"{float(issue['price']):<14.8g} {kl:<12.8g} {kh:<12.8g} {issue['issue']:<40}")
                else:
                    print(f"   {issue['symbol']:<15} {str(issue['datetime']):<22} "
                          f"{issue['price']!s:<14} {'N/A':<12} {'N/A':<12} {issue['issue']:<40}")

            if len(entry_issues) > 20:
                print(f"   ... 还有 {len(entry_issues) - 20} 个问题未显示")
        else:
            print(f"   ✅ 所有建仓价格验证通过！")

        # 平仓价格检查结果
        print(f"\n2️⃣  平仓价格检查")
        print(f"   总交易数: {total_trades}")
        print(f"   平仓原因统计:")
        for reason, count in exit_reasons.items():
            print(f"     - {reason}: {count}笔")
        print(f"   问题数: {len(exit_issues)}")

        if exit_issues:
            print(f"\n   ⚠️  发现 {len(exit_issues)} 笔平仓触发异常：\n")
            print(f"   {'交易对':<15} {'平仓时间':<20} {'平仓价':<12} {'原因':<20} {'问题':<50}")
            print(f"   {'-'*120}")

            for issue in exit_issues[:20]:  # 只显示前20个
                print(f"   {issue['symbol']:<15} {issue['datetime']:<20} "
                      f"{issue['price']:<12.6f} {issue['reason']:<20} {issue['issue']:<50}")

            if len(exit_issues) > 20:
                print(f"   ... 还有 {len(exit_issues) - 20} 个问题未显示")
        else:
            print(f"   ✅ 所有平仓触发条件验证通过！")

        # 硬止损标签一致性（全程 5m 是否曾触及 +18% 线）
        print(f"\n3️⃣  硬止损标签一致性（该止损未记为 stop_loss？）")
        print(f"   规则：做空理论止损价 = 参考建仓价 × {STOP_LOSS_PRICE_MULT:.2f}；")
        print(f"   扫描 [建仓时间, 平仓时间] 内 5m 最高价若 ≥ 该止损价，但「平仓原因」≠ stop_loss 则列出。")
        print(f"   说明：可能是 flatten/提前止损等先于硬止损，或主循环用小时收盘与 5m 极值不一致，需对照 hm1l。")
        print(f"   疑点笔数: {len(missed_sl_issues)}")

        if missed_sl_issues:
            print(f"\n   ⚠️  以下 {len(missed_sl_issues)} 笔满足「5m 曾触及 +18% 线」但非 stop_loss：\n")
            hdr = (f"   {'交易对':<14} {'实际平仓原因':<28} {'参考价':<12} {'理论止损':<12} "
                   f"{'区间最高high':<14} {'首次触止损(5m开盘)':<22}")
            print(hdr)
            print(f"   {'-'*120}")
            for issue in missed_sl_issues[:30]:
                ft = issue.get('first_touch_open_time_ms')
                if ft is not None:
                    ft_s = pd.to_datetime(ft, unit='ms').strftime('%Y-%m-%d %H:%M:%S')
                else:
                    ft_s = 'N/A'
                print(f"   {issue['symbol']:<14} {issue['exit_reason']:<28} "
                      f"{issue['baseline_entry']:<12.6f} {issue['sl_price_18pct']:<12.6f} "
                      f"{issue['max_high_5m']:<14.6f} {ft_s:<22}")
            if len(missed_sl_issues) > 30:
                print(f"   ... 还有 {len(missed_sl_issues) - 30} 笔未显示")
        else:
            print(f"   ✅ 未发现「全程触及 +18% 线却未标 stop_loss」的疑点（在有 5m 数据的交易内）。")

        # 总结
        print(f"\n{'='*80}")
        print("📋 总结")
        print(f"{'='*80}")
        print(f"总交易数: {total_trades}")
        print(f"建仓问题: {len(entry_issues)} ({len(entry_issues)/total_trades*100:.1f}%)")
        print(f"平仓问题: {len(exit_issues)} ({len(exit_issues)/total_trades*100:.1f}%)")
        print(f"硬止损标签疑点: {len(missed_sl_issues)} ({len(missed_sl_issues)/total_trades*100:.1f}%)")

        if not entry_issues and not exit_issues and not missed_sl_issues:
            print(f"\n{'🎉'*20}")
            print("✅✅✅ 回测数据完全真实可信！所有交易价格均符合5分钟K线数据！")
            print(f"{'🎉'*20}")
        else:
            print(f"\n⚠️  发现部分交易与5分钟K线（建仓首根/平仓所在根）不符或硬止损标签存在疑点，请结合回测逻辑排查")

        print(f"\n{'='*80}")

def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("用法: python jchc.py <回测报告CSV路径>")
        print("示例: python jchc.py sell_surge_backtest_report_20260208_015512.csv")
        sys.exit(1)

    csv_path = sys.argv[1]

    print(f"{'='*80}")
    print("🔍 回测核查程序 (jchc.py)")
    print(f"{'='*80}")
    print(f"CSV文件: {csv_path}")
    print(f"数据库: PostgreSQL")
    print(f"{'='*80}\n")

    checker = BacktestChecker(csv_path)
    checker.run_check()

if __name__ == '__main__':
    main()
