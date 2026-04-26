#!/usr/bin/env python3
"""
Trade Verification Script for duo-moonshot

This script verifies trade execution prices against 5-minute K-line data
to identify discrepancies between backtest results and actual market data.

Usage:
    python verify_trades.py single <symbol> <exit_time> <exit_price>
    python verify_trades.py csv <csv_file> [--column-map]
    python verify_trades.py check <symbol> <entry_time> <entry_price> <exit_time> <exit_price> <reason>
"""

import argparse
import os
import sys
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from dotenv import load_dotenv
from moonshot.db import get_postgres_db

# Load environment variables
load_dotenv()


class TradeVerifier:
    """Verifies trade execution prices against PostgreSQL K-line data."""

    def __init__(self):
        """Initialize the trade verifier."""
        self.db = None
        self.conn = None
        self.results = []

    def connect(self):
        """Establish database connection."""
        try:
            self.db = get_postgres_db().connect()
            self.conn = self.db.conn
            return True
        except Exception as e:
            print(f"❌ Database connection failed: {e}")
            return False

    def disconnect(self):
        """Close database connection."""
        if self.db:
            self.db.close()
        self.db = None
        self.conn = None

    def get_5m_kline_at_time(self, symbol: str, timestamp_ms: int) -> Optional[Tuple]:
        """
        Get 5-minute K-line that contains the given timestamp.

        Returns: (open_time, open, high, low, close) or None
        """
        if not self.conn:
            return None

        try:
            table_name = f'K5m{symbol}'
            query = f'''
                SELECT * FROM "{table_name}"
                WHERE open_time <= %s
                ORDER BY open_time DESC
                LIMIT 1
            '''
            cursor = self.conn.cursor()
            cursor.execute(query, (timestamp_ms,))
            row = cursor.fetchone()
            cursor.close()

            if row:
                # Extract first 5 columns: open_time, open, high, low, close
                return (row[0], row[1], row[2], row[3], row[4])
            return None
        except Exception as e:
            print(f"❌ Error querying {symbol}: {e}")
            return None

    def get_5m_klines_around_time(self, symbol: str, timestamp_ms: int,
                                  window_minutes: int = 60) -> List[Tuple]:
        """Get 5-minute K-lines around the given timestamp."""
        if not self.conn:
            return []

        try:
            table_name = f'K5m{symbol}'
            start_ts = timestamp_ms - window_minutes * 60 * 1000 // 2
            end_ts = timestamp_ms + window_minutes * 60 * 1000 // 2

            query = f'''
                SELECT * FROM "{table_name}"
                WHERE open_time >= %s AND open_time <= %s
                ORDER BY open_time
            '''
            cursor = self.conn.cursor()
            cursor.execute(query, (start_ts, end_ts))
            rows = cursor.fetchall()
            cursor.close()

            # Extract first 5 columns for each row
            return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]
        except Exception as e:
            print(f"❌ Error querying {symbol} around time: {e}")
            return []

    def verify_trade(self, symbol: str, entry_time: Optional[str], entry_price: Optional[float],
                    exit_time: str, exit_price: float, exit_reason: str = "unknown") -> Dict[str, Any]:
        """
        Verify a single trade execution.

        Returns detailed verification result.
        """
        if not self.connect():
            return {"error": "Database connection failed"}

        try:
            # Convert exit time to timestamp
            exit_dt = pd.to_datetime(exit_time)
            exit_ts = int(exit_dt.timestamp() * 1000)

            # Get K-line at exit time
            kline_data = self.get_5m_kline_at_time(symbol, exit_ts)

            if not kline_data:
                # Try to get nearby K-lines
                nearby_klines = self.get_5m_klines_around_time(symbol, exit_ts, 30)

                result = {
                    'symbol': symbol,
                    'exit_time': exit_time,
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'kline_found': False,
                    'error': f'No 5m K-line found at {exit_time}',
                    'nearby_klines': [
                        {
                            'time': pd.to_datetime(k[0], unit='ms').strftime('%Y-%m-%d %H:%M:%S'),
                            'low': float(k[3]),
                            'high': float(k[2]),
                            'contains_price': float(k[3]) <= exit_price <= float(k[2])
                        }
                        for k in nearby_klines[:10]  # Show first 10
                    ]
                }
                return result

            open_time, open_price, high, low, close = kline_data

            # Check if price is within K-line range
            price_in_range = float(low) <= exit_price <= float(high)

            # Get entry info if available
            entry_info = {}
            if entry_time and entry_price:
                entry_dt = pd.to_datetime(entry_time)
                entry_ts = int(entry_dt.timestamp() * 1000)
                entry_kline = self.get_5m_kline_at_time(symbol, entry_ts)

                if entry_kline:
                    _, _, entry_high, entry_low, _ = entry_kline
                    entry_in_range = float(entry_low) <= entry_price <= float(entry_high)
                    entry_info = {
                        'entry_price': entry_price,
                        'entry_kline_low': float(entry_low),
                        'entry_kline_high': float(entry_high),
                        'entry_in_range': entry_in_range
                    }

            # Get nearby K-lines for context
            nearby_klines = self.get_5m_klines_around_time(symbol, exit_ts, 60)

            result = {
                'symbol': symbol,
                'exit_time': exit_time,
                'exit_price': exit_price,
                'exit_reason': exit_reason,
                'kline_found': True,
                'kline_time': pd.to_datetime(open_time, unit='ms').strftime('%Y-%m-%d %H:%M:%S'),
                'kline_open': float(open_price),
                'kline_high': float(high),
                'kline_low': float(low),
                'kline_close': float(close),
                'price_in_range': price_in_range,
                'distance_from_low': exit_price - float(low) if exit_price >= float(low) else float(low) - exit_price,
                'distance_from_high': float(high) - exit_price if exit_price <= float(high) else exit_price - float(high),
                'percentage_deviation': abs(exit_price - float(close)) / float(close) * 100 if float(close) > 0 else 0,
                'entry_info': entry_info,
                'nearby_klines_count': len(nearby_klines),
                'analysis': self._analyze_discrepancy(exit_price, float(low), float(high), exit_reason)
            }

            self.results.append(result)
            return result

        except Exception as e:
            return {
                'symbol': symbol,
                'exit_time': exit_time,
                'exit_price': exit_price,
                'error': str(e),
                'kline_found': False
            }
        finally:
            self.disconnect()

    def _analyze_discrepancy(self, exit_price: float, kline_low: float,
                            kline_high: float, exit_reason: str) -> str:
        """Analyze why exit price might be outside K-line range."""
        if kline_low <= exit_price <= kline_high:
            return "✅ Price is within K-line range (normal execution)"

        deviation = ""
        if exit_price < kline_low:
            deviation = f"below low by {(kline_low - exit_price) / kline_low * 100:.2f}%"
        else:
            deviation = f"above high by {(exit_price - kline_high) / kline_high * 100:.2f}%"

        analysis = f"❌ Price is {deviation} outside K-line range\n"

        # Add reason-specific analysis
        if exit_reason == "stop_loss":
            analysis += "  • For stop_loss: Price may represent threshold price, not actual execution\n"
            analysis += "  • Check if SL price was calculated as entry × 1.18\n"
            analysis += "  • Actual execution might have been at K-line high\n"
        elif exit_reason == "take_profit":
            analysis += "  • For take_profit: TP price might be calculated threshold\n"
            analysis += "  • Actual execution might have been at K-line low\n"
        elif exit_reason == "max_hold_time":
            analysis += "  • For max_hold_time: Might use hourly close price, not 5m price\n"
            analysis += "  • Check if price comes from K1h table instead of K5m\n"
        elif exit_reason == "force_close":
            analysis += "  • For force_close: May use arbitrary or last known price\n"
            analysis += "  • Could be system-defined price rather than market price\n"

        analysis += "\nPossible causes:\n"
        analysis += "  1. Price source mismatch (hourly vs 5-minute data)\n"
        analysis += "  2. Threshold price used instead of actual execution\n"
        analysis += "  3. Time alignment issue (wrong 5-minute bar)\n"
        analysis += "  4. Data inconsistency between backtest and verification\n"
        analysis += "  5. Slippage or execution at worse price\n"

        return analysis

    def verify_from_csv(self, csv_path: str, column_map: Dict[str, str] = None) -> List[Dict[str, Any]]:
        """Verify multiple trades from a CSV file."""
        try:
            df = pd.read_csv(csv_path)

            # Apply column mapping if provided
            if column_map:
                df = df.rename(columns=column_map)

            results = []
            for idx, row in df.iterrows():
                if idx % 50 == 0:
                    print(f"  Processing trade {idx + 1}/{len(df)}...")

                try:
                    # Extract required fields
                    symbol = str(row.get('交易对', row.get('symbol', ''))).strip()
                    exit_time = str(row.get('平仓时间', row.get('exit_time', ''))).strip()

                    # Parse exit price
                    exit_price_str = str(row.get('平仓价格', row.get('exit_price', '0')))
                    exit_price = float(exit_price_str.replace(',', ''))

                    # Parse entry info if available
                    entry_time = None
                    entry_price = None
                    if '建仓时间' in row or 'entry_time' in row:
                        entry_time = str(row.get('建仓时间', row.get('entry_time', ''))).strip()
                        entry_price_str = str(row.get('建仓价格', row.get('entry_price', '0')))
                        entry_price = float(entry_price_str.replace(',', ''))

                    exit_reason = str(row.get('平仓原因', row.get('exit_reason', 'unknown'))).strip()

                    if not symbol or not exit_time:
                        continue

                    result = self.verify_trade(symbol, entry_time, entry_price,
                                               exit_time, exit_price, exit_reason)
                    results.append(result)

                except Exception as e:
                    print(f"❌ Error processing row {idx}: {e}")
                    continue

            return results

        except Exception as e:
            print(f"❌ Error reading CSV file: {e}")
            return []

    def print_result(self, result: Dict[str, Any], verbose: bool = True):
        """Print verification result in readable format."""
        print("\n" + "="*80)
        print(f"🔍 Trade Verification: {result.get('symbol', 'Unknown')}")
        print("="*80)

        if 'error' in result:
            print(f"❌ Error: {result['error']}")
            return

        print(f"Symbol: {result['symbol']}")
        print(f"Exit Time: {result['exit_time']}")
        print(f"Exit Price: {result['exit_price']:.6f}")
        print(f"Exit Reason: {result['exit_reason']}")

        if not result.get('kline_found', False):
            print(f"❌ No 5m K-line found at exit time")
            if 'nearby_klines' in result and result['nearby_klines']:
                print("\nNearby K-lines:")
                for kline in result['nearby_klines'][:5]:
                    print(f"  {kline['time']}: [{kline['low']:.6f}, {kline['high']:.6f}] - "
                          f"Contains price: {kline['contains_price']}")
            return

        print(f"5m K-line Time: {result['kline_time']}")
        print(f"K-line Range: [{result['kline_low']:.6f}, {result['kline_high']:.6f}]")
        print(f"K-line Close: {result['kline_close']:.6f}")

        if result['price_in_range']:
            print(f"✅ Price is WITHIN K-line range")
            print(f"   Distance to low: {result['distance_from_low']:.6f}")
            print(f"   Distance to high: {result['distance_from_high']:.6f}")
        else:
            print(f"❌ Price is OUTSIDE K-line range!")
            if result['exit_price'] < result['kline_low']:
                print(f"   Below low by: {result['distance_from_low']:.6f} "
                      f"({result['distance_from_low']/result['kline_low']*100:.2f}%)")
            else:
                print(f"   Above high by: {result['distance_from_high']:.6f} "
                      f"({result['distance_from_high']/result['kline_high']*100:.2f}%)")

        print(f"Percentage deviation from close: {result['percentage_deviation']:.2f}%")

        # Print entry info if available
        if result.get('entry_info'):
            entry = result['entry_info']
            print(f"\nEntry Price: {entry['entry_price']:.6f}")
            print(f"Entry K-line Range: [{entry['entry_kline_low']:.6f}, {entry['entry_kline_high']:.6f}]")
            print(f"Entry Price in Range: {'✅' if entry['entry_in_range'] else '❌'}")

        print(f"\n{result.get('analysis', '')}")

        if verbose and result.get('nearby_klines_count', 0) > 0:
            print(f"\nFound {result['nearby_klines_count']} K-lines within ±30 minutes")

        print("="*80)

    def print_summary(self, results: List[Dict[str, Any]]):
        """Print summary of verification results."""
        if not results:
            print("No results to summarize")
            return

        print("\n" + "="*80)
        print("📊 Verification Summary")
        print("="*80)

        total = len(results)
        kline_found = sum(1 for r in results if r.get('kline_found', False))
        in_range = sum(1 for r in results if r.get('price_in_range', False))

        print(f"Total trades verified: {total}")
        print(f"Trades with K-line data: {kline_found} ({kline_found/total*100:.1f}%)")
        print(f"Trades within K-line range: {in_range} ({in_range/kline_found*100:.1f}% of those with data)")

        if kline_found > 0:
            # Group by exit reason
            reason_stats = {}
            for r in results:
                if r.get('kline_found', False):
                    reason = r.get('exit_reason', 'unknown')
                    in_range = r.get('price_in_range', False)

                    if reason not in reason_stats:
                        reason_stats[reason] = {'total': 0, 'in_range': 0}

                    reason_stats[reason]['total'] += 1
                    if in_range:
                        reason_stats[reason]['in_range'] += 1

            print("\nBy Exit Reason:")
            for reason, stats in sorted(reason_stats.items()):
                total_r = stats['total']
                in_range_r = stats['in_range']
                pct = in_range_r / total_r * 100 if total_r > 0 else 0
                print(f"  {reason}: {in_range_r}/{total_r} in range ({pct:.1f}%)")

        # Identify worst offenders
        outliers = []
        for r in results:
            if r.get('kline_found', False) and not r.get('price_in_range', False):
                deviation = r.get('percentage_deviation', 0)
                if deviation > 1.0:  # More than 1% deviation
                    outliers.append({
                        'symbol': r['symbol'],
                        'deviation': deviation,
                        'reason': r.get('exit_reason', 'unknown')
                    })

        if outliers:
            print(f"\n⚠️  Significant outliers (>1% deviation): {len(outliers)} trades")
            outliers.sort(key=lambda x: x['deviation'], reverse=True)
            for outlier in outliers[:10]:
                print(f"  {outlier['symbol']}: {outlier['deviation']:.2f}% deviation ({outlier['reason']})")

        print("="*80)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Verify trade execution prices against K-line data")
    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Single trade verification
    single_parser = subparsers.add_parser('single', help='Verify single trade')
    single_parser.add_argument('symbol', help='Trading symbol (e.g., BTCUSDT)')
    single_parser.add_argument('exit_time', help='Exit time (YYYY-MM-DD HH:MM:SS)')
    single_parser.add_argument('exit_price', type=float, help='Exit price')
    single_parser.add_argument('--entry-time', help='Entry time (optional)')
    single_parser.add_argument('--entry-price', type=float, help='Entry price (optional)')
    single_parser.add_argument('--reason', default='unknown', help='Exit reason (optional)')

    # CSV batch verification
    csv_parser = subparsers.add_parser('csv', help='Verify trades from CSV file')
    csv_parser.add_argument('csv_file', help='Path to CSV file')
    csv_parser.add_argument('--symbol-col', default='交易对', help='Symbol column name')
    csv_parser.add_argument('--exit-time-col', default='平仓时间', help='Exit time column name')
    csv_parser.add_argument('--exit-price-col', default='平仓价格', help='Exit price column name')
    csv_parser.add_argument('--exit-reason-col', default='平仓原因', help='Exit reason column name')
    csv_parser.add_argument('--entry-time-col', default='建仓时间', help='Entry time column name')
    csv_parser.add_argument('--entry-price-col', default='建仓价格', help='Entry price column name')

    # Manual check with all parameters
    check_parser = subparsers.add_parser('check', help='Manual check with all parameters')
    check_parser.add_argument('symbol', help='Trading symbol')
    check_parser.add_argument('entry_time', help='Entry time')
    check_parser.add_argument('entry_price', type=float, help='Entry price')
    check_parser.add_argument('exit_time', help='Exit time')
    check_parser.add_argument('exit_price', type=float, help='Exit price')
    check_parser.add_argument('reason', help='Exit reason')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    verifier = TradeVerifier()

    if args.command == 'single':
        result = verifier.verify_trade(
            symbol=args.symbol,
            entry_time=args.entry_time,
            entry_price=args.entry_price,
            exit_time=args.exit_time,
            exit_price=args.exit_price,
            exit_reason=args.reason
        )
        verifier.print_result(result, verbose=True)

    elif args.command == 'csv':
        # Create column mapping
        column_map = {
            '交易对': args.symbol_col,
            '平仓时间': args.exit_time_col,
            '平仓价格': args.exit_price_col,
            '平仓原因': args.exit_reason_col,
            '建仓时间': args.entry_time_col,
            '建仓价格': args.entry_price_col
        }

        print(f"Verifying trades from {args.csv_file}...")
        results = verifier.verify_from_csv(args.csv_file, column_map)

        # Print results
        for i, result in enumerate(results[:20]):  # Show first 20
            print(f"\nTrade {i + 1}:")
            verifier.print_result(result, verbose=False)

        if len(results) > 20:
            print(f"\n... and {len(results) - 20} more trades")

        # Print summary
        verifier.print_summary(results)

    elif args.command == 'check':
        result = verifier.verify_trade(
            symbol=args.symbol,
            entry_time=args.entry_time,
            entry_price=args.entry_price,
            exit_time=args.exit_time,
            exit_price=args.exit_price,
            exit_reason=args.reason
        )
        verifier.print_result(result, verbose=True)


if __name__ == '__main__':
    main()
