#!/usr/bin/env python3
"""
Diagnostic Report and Fix Plan for hm1l20260418.py Price Issues

This script analyzes price discrepancies between backtest execution prices
and actual 5-minute K-line data, providing detailed diagnosis and fix recommendations.

Usage:
    python diagnose_price_issues.py <csv_file> [--output report.md]
    python diagnose_price_issues.py analyze <symbol> <exit_time> <exit_price> <reason>
"""

import argparse
import json
import os
import pandas as pd
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from dotenv import load_dotenv
from moonshot.db import get_postgres_db

# Load environment variables
load_dotenv()


class PriceIssueDiagnoser:
    """Diagnoses price discrepancies in backtest executions."""

    def __init__(self):
        """Initialize the diagnoser."""
        self.db = None
        self.conn = None
        self.issues_found = []
        self.patterns_identified = []

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

    def get_kline_data(self, symbol: str, timestamp_ms: int,
                       kline_type: str = '5m') -> Optional[Tuple]:
        """
        Get K-line data at specified timestamp.

        Args:
            symbol: Trading symbol
            timestamp_ms: Timestamp in milliseconds
            kline_type: '5m' for 5-minute, '1h' for hourly

        Returns: (open_time, open, high, low, close) or None
        """
        if not self.conn:
            return None

        try:
            if kline_type == '5m':
                table_name = f'K5m{symbol}'
            elif kline_type == '1h':
                table_name = f'K1h{symbol}'
            else:
                return None

            # Get the K-line containing the timestamp
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
                # Extract first 5 columns
                return (row[0], row[1], row[2], row[3], row[4])
            return None
        except Exception as e:
            print(f"❌ Error querying {symbol} {kline_type}: {e}")
            return None

    def diagnose_trade(self, symbol: str, entry_price: float, exit_time: str,
                      exit_price: float, exit_reason: str) -> Dict[str, Any]:
        """
        Diagnose a single trade's price issues.

        Returns detailed diagnosis with root cause analysis.
        """
        if not self.connect():
            return {"error": "Database connection failed"}

        try:
            # Convert times
            exit_dt = pd.to_datetime(exit_time)
            exit_ts = int(exit_dt.timestamp() * 1000)

            # Get both 5m and 1h K-line data
            kline_5m = self.get_kline_data(symbol, exit_ts, '5m')
            kline_1h = self.get_kline_data(symbol, exit_ts, '1h')

            diagnosis = {
                'symbol': symbol,
                'entry_price': entry_price,
                'exit_time': exit_time,
                'exit_price': exit_price,
                'exit_reason': exit_reason,
                'has_5m_data': kline_5m is not None,
                'has_1h_data': kline_1h is not None,
                'issues': [],
                'root_causes': [],
                'fix_recommendations': []
            }

            # Calculate threshold prices
            sl_threshold = entry_price * 1.18  # 18% stop loss
            tp_33_threshold = entry_price * 0.67  # 33% take profit (price drop)
            tp_21_threshold = entry_price * 0.79  # 21% take profit
            tp_10_threshold = entry_price * 0.90  # 10% take profit

            # Analyze based on exit reason
            if exit_reason == 'stop_loss':
                diagnosis = self._diagnose_stop_loss(
                    diagnosis, entry_price, exit_price, sl_threshold, kline_5m, kline_1h
                )
            elif exit_reason == 'take_profit':
                diagnosis = self._diagnose_take_profit(
                    diagnosis, entry_price, exit_price,
                    tp_33_threshold, tp_21_threshold, tp_10_threshold,
                    kline_5m, kline_1h
                )
            elif exit_reason == 'max_hold_time':
                diagnosis = self._diagnose_max_hold_time(
                    diagnosis, exit_price, kline_5m, kline_1h
                )
            elif exit_reason == 'force_close':
                diagnosis = self._diagnose_force_close(
                    diagnosis, exit_price, kline_5m, kline_1h
                )
            else:
                diagnosis = self._diagnose_general(
                    diagnosis, exit_price, kline_5m, kline_1h
                )

            # Add to issues found
            if diagnosis['issues']:
                self.issues_found.append(diagnosis)

            return diagnosis

        except Exception as e:
            return {
                'symbol': symbol,
                'error': str(e),
                'issues': [f'Diagnosis error: {e}']
            }
        finally:
            self.disconnect()

    def _diagnose_stop_loss(self, diagnosis: Dict, entry_price: float,
                           exit_price: float, sl_threshold: float,
                           kline_5m: Optional[Tuple], kline_1h: Optional[Tuple]) -> Dict:
        """Diagnose stop loss issues."""

        # Check if using threshold price
        if abs(exit_price - sl_threshold) < 0.000001:
            diagnosis['issues'].append("Using threshold price (entry × 1.18) instead of actual execution")
            diagnosis['root_causes'].append("_resolve_short_exit_5m_in_hour returns sl_price directly")
            diagnosis['fix_recommendations'].append(
                "Modify stop loss execution to use K-line high price instead of threshold"
            )

        # Check 5m K-line compatibility
        if kline_5m:
            _, _, high, low, _ = kline_5m
            high = float(high)
            low = float(low)

            if exit_price < low or exit_price > high:
                diagnosis['issues'].append(f"Exit price {exit_price:.6f} outside 5m K-line range [{low:.6f}, {high:.6f}]")

                if exit_price > high:
                    diagnosis['root_causes'].append("Threshold price exceeds K-line maximum")
                    diagnosis['fix_recommendations'].append(
                        "When threshold > K-line high, execute at K-line high price"
                    )

        # Check 1h K-line compatibility
        if kline_1h:
            _, _, h_high, h_low, h_close = kline_1h
            h_high = float(h_high)
            h_close = float(h_close)

            if abs(exit_price - h_close) < 0.000001:
                diagnosis['issues'].append("Using hourly close price instead of 5m price")
                diagnosis['root_causes'].append("Price source mismatch between hourly and 5-minute data")
                diagnosis['fix_recommendations'].append(
                    "Ensure all executions use 5-minute K-line data for price validation"
                )

        return diagnosis

    def _diagnose_take_profit(self, diagnosis: Dict, entry_price: float,
                             exit_price: float, tp_33: float, tp_21: float,
                             tp_10: float, kline_5m: Optional[Tuple],
                             kline_1h: Optional[Tuple]) -> Dict:
        """Diagnose take profit issues."""

        # Check which threshold was used
        thresholds = [
            ('33%', tp_33, abs(exit_price - tp_33)),
            ('21%', tp_21, abs(exit_price - tp_21)),
            ('10%', tp_10, abs(exit_price - tp_10))
        ]
        thresholds.sort(key=lambda x: x[2])
        closest_threshold = thresholds[0]

        if closest_threshold[2] < 0.000001:
            diagnosis['issues'].append(f"Using {closest_threshold[0]} threshold price instead of actual execution")
            diagnosis['root_causes'].append("_resolve_short_exit_5m_in_hour returns tp_price directly")
            diagnosis['fix_recommendations'].append(
                "Modify take profit execution to use K-line low price instead of threshold"
            )

        # Check 5m K-line compatibility
        if kline_5m:
            _, _, high, low, _ = kline_5m
            low = float(low)

            if exit_price < low:
                diagnosis['issues'].append(f"Exit price {exit_price:.6f} below 5m K-line low {low:.6f}")
                diagnosis['root_causes'].append("Threshold price below K-line minimum")
                diagnosis['fix_recommendations'].append(
                    "When threshold < K-line low, execute at K-line low price"
                )

        return diagnosis

    def _diagnose_max_hold_time(self, diagnosis: Dict, exit_price: float,
                               kline_5m: Optional[Tuple], kline_1h: Optional[Tuple]) -> Dict:
        """Diagnose max hold time issues."""

        if kline_1h and kline_5m:
            _, _, h_high, h_low, h_close = kline_1h
            _, _, _, m_low, m_close = kline_5m

            h_close = float(h_close)
            m_close = float(m_close)

            # Check if using hourly close instead of 5m close
            if abs(exit_price - h_close) < abs(exit_price - m_close) * 0.1:
                diagnosis['issues'].append("Using hourly close price for max_hold_time execution")
                diagnosis['root_causes'].append("check_exit_conditions uses row['close'] from hourly data")
                diagnosis['fix_recommendations'].append(
                    "For max_hold_time, use 5-minute K-line close price instead of hourly"
                )

        # Check 5m K-line compatibility
        if kline_5m:
            _, _, high, low, close = kline_5m
            low = float(low)
            high = float(high)

            if exit_price < low or exit_price > high:
                diagnosis['issues'].append(f"Exit price outside 5m K-line range")
                diagnosis['fix_recommendations'].append(
                    "Align max_hold_time execution price with 5m K-line boundaries"
                )

        return diagnosis

    def _diagnose_force_close(self, diagnosis: Dict, exit_price: float,
                             kline_5m: Optional[Tuple], kline_1h: Optional[Tuple]) -> Dict:
        """Diagnose force close issues."""

        diagnosis['issues'].append("force_close may use arbitrary or system-defined prices")
        diagnosis['root_causes'].append("Force close logic may not validate against market data")
        diagnosis['fix_recommendations'].append(
            "Implement price validation for force_close executions"
        )

        return diagnosis

    def _diagnose_general(self, diagnosis: Dict, exit_price: float,
                         kline_5m: Optional[Tuple], kline_1h: Optional[Tuple]) -> Dict:
        """Diagnose general price issues."""

        if kline_5m:
            _, _, high, low, close = kline_5m
            low = float(low)
            high = float(high)

            if exit_price < low or exit_price > high:
                diagnosis['issues'].append("Price outside valid K-line range")
                diagnosis['root_causes'].append("Price source or execution timing issue")
                diagnosis['fix_recommendations'].append(
                    "Verify price source and execution time alignment"
                )

        return diagnosis

    def analyze_csv(self, csv_path: str) -> List[Dict[str, Any]]:
        """Analyze all trades in a CSV file."""
        try:
            df = pd.read_csv(csv_path)
            diagnoses = []

            for idx, row in df.iterrows():
                if idx % 50 == 0:
                    print(f"  Analyzing trade {idx + 1}/{len(df)}...")

                try:
                    symbol = str(row.get('交易对', row.get('symbol', ''))).strip()
                    exit_time = str(row.get('平仓具体时间', row.get('平仓时间', ''))).strip()

                    # Parse prices
                    entry_price_str = str(row.get('建仓价', row.get('建仓价格', '0')))
                    entry_price = float(entry_price_str.replace(',', ''))

                    exit_price_str = str(row.get('平仓价', row.get('平仓价格', '0')))
                    exit_price = float(exit_price_str.replace(',', ''))

                    exit_reason = str(row.get('平仓原因', row.get('exit_reason', 'unknown'))).strip()

                    if not symbol or not exit_time:
                        continue

                    diagnosis = self.diagnose_trade(
                        symbol, entry_price, exit_time, exit_price, exit_reason
                    )
                    diagnoses.append(diagnosis)

                except Exception as e:
                    print(f"❌ Error analyzing row {idx}: {e}")
                    continue

            return diagnoses

        except Exception as e:
            print(f"❌ Error reading CSV: {e}")
            return []

    def generate_fix_plan(self, diagnoses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate comprehensive fix plan based on diagnoses."""

        # Count issues by type
        issue_counts = {}
        root_cause_counts = {}
        fix_counts = {}

        for diagnosis in diagnoses:
            for issue in diagnosis.get('issues', []):
                issue_counts[issue] = issue_counts.get(issue, 0) + 1

            for cause in diagnosis.get('root_causes', []):
                root_cause_counts[cause] = root_cause_counts.get(cause, 0) + 1

            for fix in diagnosis.get('fix_recommendations', []):
                fix_counts[fix] = fix_counts.get(fix, 0) + 1

        # Identify patterns
        patterns = []

        # Pattern 1: Threshold price usage
        threshold_issues = [k for k in issue_counts.keys()
                          if 'threshold' in k.lower() or 'entry ×' in k]
        if threshold_issues:
            patterns.append({
                'name': 'Threshold Price Execution',
                'description': 'Using calculated threshold prices instead of actual market execution',
                'affected_functions': ['_resolve_short_exit_5m_in_hour', 'check_exit_conditions'],
                'severity': 'HIGH',
                'trades_affected': sum(issue_counts.get(k, 0) for k in threshold_issues)
            })

        # Pattern 2: Price source mismatch
        source_issues = [k for k in issue_counts.keys()
                        if 'hourly' in k.lower() or '5m' in k.lower() or 'close price' in k]
        if source_issues:
            patterns.append({
                'name': 'Price Source Mismatch',
                'description': 'Using hourly K-line prices instead of 5-minute K-line prices',
                'affected_functions': ['check_exit_conditions (max_hold_time)'],
                'severity': 'MEDIUM',
                'trades_affected': sum(issue_counts.get(k, 0) for k in source_issues)
            })

        # Pattern 3: Price validation missing
        validation_issues = [k for k in issue_counts.keys()
                           if 'outside' in k.lower() or 'range' in k.lower()]
        if validation_issues:
            patterns.append({
                'name': 'Missing Price Validation',
                'description': 'Executing trades at prices outside valid K-line ranges',
                'affected_functions': ['exit_position', 'check_exit_conditions'],
                'severity': 'HIGH',
                'trades_affected': sum(issue_counts.get(k, 0) for k in validation_issues)
            })

        self.patterns_identified = patterns

        # Create fix plan
        fix_plan = {
            'summary': {
                'total_trades_analyzed': len(diagnoses),
                'total_issues_found': sum(len(d.get('issues', [])) for d in diagnoses),
                'patterns_identified': len(patterns),
                'high_severity_issues': len([p for p in patterns if p['severity'] == 'HIGH'])
            },
            'patterns': patterns,
            'detailed_fixes': [
                {
                    'function': '_resolve_short_exit_5m_in_hour',
                    'problem': 'Returns threshold prices (sl_price, tp_price) directly',
                    'solution': 'Return actual K-line execution prices (high for SL, low for TP)',
                    'code_changes': [
                        "Replace `return self._exit_dt_str_from_open_time_ms(ot)` with price validation",
                        "Add price clamping to K-line boundaries",
                        "Return execution price along with exit time"
                    ]
                },
                {
                    'function': 'check_exit_conditions',
                    'problem': 'Uses hourly close price for max_hold_time',
                    'solution': 'Use 5-minute K-line close price for all executions',
                    'code_changes': [
                        "Change `exit_price = row['close']` to query 5m K-line",
                        "Add price validation before calling exit_position",
                        "Ensure time alignment with 5-minute boundaries"
                    ]
                },
                {
                    'function': 'exit_position',
                    'problem': 'Accepts any price without validation',
                    'solution': 'Add price validation against 5m K-line data',
                    'code_changes': [
                        "Add validation method `_validate_execution_price`",
                        "Clamp prices to K-line boundaries if needed",
                        "Log price adjustments for audit trail"
                    ]
                }
            ],
            'implementation_priority': [
                "1. Fix _resolve_short_exit_5m_in_hour to use actual execution prices",
                "2. Update check_exit_conditions to use 5m prices for max_hold_time",
                "3. Add price validation in exit_position",
                "4. Update backtest reports to reflect actual execution prices"
            ],
            'testing_plan': [
                "Run jchc.py on fixed code to verify all trades pass validation",
                "Compare P&L before and after fixes (should be minimal difference)",
                "Verify threshold logic still works correctly",
                "Test edge cases (prices at K-line boundaries)"
            ]
        }

        return fix_plan

    def print_diagnosis(self, diagnosis: Dict[str, Any]):
        """Print diagnosis in readable format."""
        print("\n" + "="*80)
        print(f"🔍 Diagnosis: {diagnosis['symbol']} - {diagnosis['exit_reason']}")
        print("="*80)

        print(f"Symbol: {diagnosis['symbol']}")
        print(f"Entry Price: {diagnosis['entry_price']:.6f}")
        print(f"Exit Time: {diagnosis['exit_time']}")
        print(f"Exit Price: {diagnosis['exit_price']:.6f}")
        print(f"Exit Reason: {diagnosis['exit_reason']}")

        if not diagnosis.get('has_5m_data', False):
            print("❌ No 5-minute K-line data available")

        if diagnosis.get('issues'):
            print(f"\n⚠️  Issues Found ({len(diagnosis['issues'])}):")
            for issue in diagnosis['issues']:
                print(f"  • {issue}")

        if diagnosis.get('root_causes'):
            print(f"\n🔍 Root Causes:")
            for cause in diagnosis['root_causes']:
                print(f"  • {cause}")

        if diagnosis.get('fix_recommendations'):
            print(f"\n🔧 Fix Recommendations:")
            for fix in diagnosis['fix_recommendations']:
                print(f"  • {fix}")

        print("="*80)

    def print_fix_plan(self, fix_plan: Dict[str, Any], output_file: Optional[str] = None):
        """Print or save comprehensive fix plan."""

        report = []
        report.append("# Price Issue Fix Plan for hm1l20260418.py")
        report.append("")
        report.append("## Executive Summary")
        report.append("")
        report.append(f"- **Trades Analyzed**: {fix_plan['summary']['total_trades_analyzed']}")
        report.append(f"- **Total Issues Found**: {fix_plan['summary']['total_issues_found']}")
        report.append(f"- **Patterns Identified**: {fix_plan['summary']['patterns_identified']}")
        report.append(f"- **High Severity Issues**: {fix_plan['summary']['high_severity_issues']}")
        report.append("")

        report.append("## Problem Patterns Identified")
        report.append("")
        for pattern in fix_plan['patterns']:
            report.append(f"### {pattern['name']} ({pattern['severity']})")
            report.append(f"- **Description**: {pattern['description']}")
            report.append(f"- **Affected Functions**: {', '.join(pattern['affected_functions'])}")
            report.append(f"- **Trades Affected**: {pattern['trades_affected']}")
            report.append("")

        report.append("## Detailed Fixes")
        report.append("")
        for fix in fix_plan['detailed_fixes']:
            report.append(f"### {fix['function']}")
            report.append(f"- **Problem**: {fix['problem']}")
            report.append(f"- **Solution**: {fix['solution']}")
            report.append("- **Code Changes**:")
            for change in fix['code_changes']:
                report.append(f"  - {change}")
            report.append("")

        report.append("## Implementation Priority")
        report.append("")
        for item in fix_plan['implementation_priority']:
            report.append(item)
        report.append("")

        report.append("## Testing Plan")
        report.append("")
        for item in fix_plan['testing_plan']:
            report.append(f"- {item}")
        report.append("")

        report.append("## Expected Impact")
        report.append("")
        report.append("1. **Data Consistency**: All executions will align with 5-minute K-line data")
        report.append("2. **Realism Improvement**: Prices will reflect actual market execution")
        report.append("3. **Validation Pass**: jchc.py will show significantly fewer issues")
        report.append("4. **P&L Accuracy**: Minimal impact on overall profitability")
        report.append("")

        report.append("## Code Changes Overview")
        report.append("")
        report.append("```python")
        report.append("# 1. Modified _resolve_short_exit_5m_in_hour")
        report.append("def _resolve_short_exit_5m_in_hour(self, symbol, hour_datetime, tp_price, sl_price, mode):")
        report.append("    # Returns (exit_time, execution_price) instead of just exit_time")
        report.append("    # Execution price is clamped to K-line boundaries")
        report.append("    # - SL: min(sl_price, K_line_high)")
        report.append("    # - TP: max(tp_price, K_line_low)")
        report.append("")
        report.append("# 2. Modified check_exit_conditions for max_hold_time")
        report.append("def check_exit_conditions(self, position, current_date, current_price):")
        report.append("    # For max_hold_time, query 5m K-line instead of using hourly close")
        report.append("    exit_price = get_5m_close_at_time(symbol, exit_time)")
        report.append("")
        report.append("# 3. New validation method")
        report.append("def _validate_execution_price(self, symbol, target_price, execution_time):")
        report.append("    # Clamps price to K-line [low, high] range")
        report.append("    # Logs adjustments for audit trail")
        report.append("```")

        full_report = "\n".join(report)

        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(full_report)
            print(f"✅ Fix plan saved to: {output_file}")
        else:
            print(full_report)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Diagnose price issues in hm1l20260418.py")
    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # CSV analysis
    csv_parser = subparsers.add_parser('csv', help='Analyze CSV file')
    csv_parser.add_argument('csv_file', help='Path to CSV file')
    csv_parser.add_argument('--output', help='Output report file (optional)')

    # Single trade analysis
    single_parser = subparsers.add_parser('analyze', help='Analyze single trade')
    single_parser.add_argument('symbol', help='Trading symbol')
    single_parser.add_argument('exit_time', help='Exit time (YYYY-MM-DD HH:MM:SS)')
    single_parser.add_argument('exit_price', type=float, help='Exit price')
    single_parser.add_argument('reason', help='Exit reason')
    single_parser.add_argument('--entry-price', type=float, default=0, help='Entry price')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    diagnoser = PriceIssueDiagnoser()

    if args.command == 'csv':
        print(f"Analyzing trades from {args.csv_file}...")
        diagnoses = diagnoser.analyze_csv(args.csv_file)

        # Print sample diagnoses
        print(f"\nAnalyzed {len(diagnoses)} trades")
        for i, diagnosis in enumerate(diagnoses[:10]):  # Show first 10
            if diagnosis.get('issues'):
                diagnoser.print_diagnosis(diagnosis)

        # Generate and print fix plan
        fix_plan = diagnoser.generate_fix_plan(diagnoses)
        diagnoser.print_fix_plan(fix_plan, args.output)

    elif args.command == 'analyze':
        diagnosis = diagnoser.diagnose_trade(
            args.symbol, args.entry_price, args.exit_time,
            args.exit_price, args.reason
        )
        diagnoser.print_diagnosis(diagnosis)


if __name__ == '__main__':
    main()
