#!/usr/bin/env python3
"""
SQLite to PostgreSQL Code Migration Tool for duo-moonshot

This tool helps migrate Python code from SQLite to PostgreSQL syntax by:
1. Converting SQL placeholders (? → %s)
2. Adding double quotes around dynamic table names
3. Updating system table queries (sqlite_master → information_schema.tables)
4. Adapting table existence checks
5. Handling funding rate table structure changes
6. Updating database connection code

Usage:
    python convert_sqlite_to_postgresql.py scan <file_or_directory>
    python convert_sqlite_to_postgresql.py convert <file_or_directory> [--dry-run] [--backup]
"""

import argparse
import ast
import codecs
import fnmatch
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class CodePattern:
    """A pattern to match SQLite code that needs conversion."""
    name: str
    description: str
    sqlite_pattern: str  # Regex pattern
    postgresql_replacement: str  # Replacement pattern
    flags: int = re.MULTILINE | re.DOTALL


@dataclass
class ImportPattern:
    """Pattern for import statements that need updating."""
    name: str
    description: str
    sqlite_import: str
    postgresql_import: str


@dataclass
class FileChange:
    """Record of a change made to a file."""
    file_path: str
    line_number: int
    old_text: str
    new_text: str
    pattern_name: str


class SQLiteToPostgreSQLConverter:
    """Main converter class for SQLite to PostgreSQL code migration."""

    def __init__(self):
        # Define code patterns for SQLite to PostgreSQL conversion
        self.patterns = [
            # Pattern 1: SQL placeholders (? → %s)
            CodePattern(
                name="sql_placeholder",
                description="Convert SQLite placeholders to PostgreSQL",
                sqlite_pattern=r'(\bcursor\.execute\s*\([^)]*)\?([^)]*\))',
                postgresql_replacement=r'\1%s\2'
            ),
            CodePattern(
                name="sql_placeholder_multi",
                description="Convert multiple SQLite placeholders",
                sqlite_pattern=r'(\bcursor\.execute\s*\([^?)]*)\?([^?)]*)\?([^)]*\))',
                postgresql_replacement=r'\1%s\2%s\3'
            ),

            # Pattern 2: Dynamic table names without quotes
            CodePattern(
                name="dynamic_table_no_quotes",
                description="Add double quotes around dynamic table names in FROM/JOIN",
                sqlite_pattern=r'(\bFROM\s+)(\w+\{[^}]*\}\w*|\w+_\w+)',
                postgresql_replacement=r'\1"\2"'
            ),
            CodePattern(
                name="dynamic_table_fstring_no_quotes",
                description="Add double quotes around f-string table names",
                sqlite_pattern=r'(\bFROM\s+)(f\'[^\']*\'|f\"[^\"]*\")',
                postgresql_replacement=r'\1"\2"'
            ),

            # Pattern 3: System table queries
            CodePattern(
                name="system_table_sqlite_master",
                description="Convert sqlite_master to information_schema.tables",
                sqlite_pattern=r'(\b)sqlite_master(\b)',
                postgresql_replacement=r'\1information_schema.tables\2'
            ),
            CodePattern(
                name="table_existence_check",
                description="Update table existence check query",
                sqlite_pattern=r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type=['\"]table['\"]\s+AND\s+name=\?",
                postgresql_replacement=r"SELECT table_name FROM information_schema.tables WHERE table_name = %s"
            ),

            # Pattern 4: Funding rate table structure
            CodePattern(
                name="funding_rate_unified_table",
                description="Convert unified funding_rate_history to per-symbol tables",
                sqlite_pattern=r'FROM\s+funding_rate_history\s+WHERE\s+symbol\s*=\s*%s',
                postgresql_replacement=r'FROM "FR{symbol}" WHERE funding_time >= %s AND funding_time < %s'
            ),
            CodePattern(
                name="funding_rate_timestamp_conversion",
                description="Add timestamp conversion for funding rate queries",
                sqlite_pattern=r'entry_datetime\.strftime\([\'\"][^)]*[\'\"]\)',
                postgresql_replacement=r'self.datetime_to_timestamp(entry_datetime)'
            ),

            # Pattern 5: Database connection
            CodePattern(
                name="sqlite_import",
                description="Replace sqlite3 import with PostgreSQL connection",
                sqlite_pattern=r'^\s*import\s+sqlite3\s*$',
                postgresql_replacement=r'from moonshot.db import get_postgres_db'
            ),
            CodePattern(
                name="sqlite_connect",
                description="Replace sqlite3.connect with PostgreSQL connection",
                sqlite_pattern=r'(\w+)\s*=\s*sqlite3\.connect\([^)]*\)',
                postgresql_replacement=r'\1 = get_postgres_db().connect().conn'
            ),

            # Pattern 6: Table name in f-string without quotes
            CodePattern(
                name="fstring_table_in_query",
                description="Add quotes around f-string table names in SQL queries",
                sqlite_pattern=r'(SELECT|INSERT|UPDATE|DELETE|FROM|INTO|JOIN)\s+(f\'[^\']*\'|f\"[^\"]*\")',
                postgresql_replacement=r'\1 "\2"'
            ),

            # Pattern 7: PRAGMA table_info
            CodePattern(
                name="pragma_table_info",
                description="Convert PRAGMA table_info to information_schema.columns",
                sqlite_pattern=r'PRAGMA\s+table_info\((\w+)\)',
                postgresql_replacement=r"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '\1' ORDER BY ordinal_position"
            ),
        ]

        # Import patterns
        self.import_patterns = [
            ImportPattern(
                name="replace_sqlite_import",
                description="Replace sqlite3 import with PostgreSQL imports",
                sqlite_import="import sqlite3",
                postgresql_import="from moonshot.db import get_postgres_db"
            ),
            ImportPattern(
                name="add_postgres_import",
                description="Add psycopg2 import if needed",
                sqlite_import="# PostgreSQL imports",
                postgresql_import="import psycopg2\nfrom psycopg2.extras import DictCursor"
            ),
        ]

        # File patterns to scan
        self.file_patterns = ['*.py']

        # Directories to exclude
        self.exclude_dirs = ['.git', '__pycache__', '.venv', 'venv', 'node_modules', '.idea', '.vscode']

        # Files to exclude
        self.exclude_files = ['convert_sqlite_to_postgresql.py', 'migrate_sqlite_to_postgresql.py']

    def find_files(self, path: str) -> List[str]:
        """Find all Python files in the given path."""
        files = []
        path_obj = Path(path)

        if path_obj.is_file():
            if path_obj.suffix == '.py' and path_obj.name not in self.exclude_files:
                files.append(str(path_obj))
        else:
            for root, dirs, filenames in os.walk(path):
                # Skip excluded directories
                dirs[:] = [d for d in dirs if d not in self.exclude_dirs]

                for filename in filenames:
                    filepath = Path(root) / filename
                    if any(fnmatch.fnmatch(filename, pattern) for pattern in self.file_patterns):
                        if filename not in self.exclude_files:
                            files.append(str(filepath))

        return files

    def read_file(self, file_path: str) -> str:
        """Read file content with proper encoding."""
        try:
            with codecs.open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            with codecs.open(file_path, 'r', encoding='latin-1') as f:
                return f.read()

    def write_file(self, file_path: str, content: str) -> None:
        """Write content to file with proper encoding."""
        with codecs.open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def backup_file(self, file_path: str) -> str:
        """Create a backup of the file."""
        backup_path = f"{file_path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(file_path, backup_path)
        logger.info(f"Created backup: {backup_path}")
        return backup_path

    def scan_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Scan a file for SQLite patterns that need conversion."""
        content = self.read_file(file_path)
        findings = []

        for pattern in self.patterns:
            try:
                matches = list(re.finditer(pattern.sqlite_pattern, content, pattern.flags))
                if matches:
                    for match in matches:
                        # Get line number
                        line_number = content[:match.start()].count('\n') + 1

                        # Get context
                        start = max(0, match.start() - 50)
                        end = min(len(content), match.end() + 50)
                        context = content[start:end].replace('\n', ' ')
                        if len(context) > 100:
                            context = context[:100] + "..."

                        findings.append({
                            'file': file_path,
                            'line': line_number,
                            'pattern': pattern.name,
                            'description': pattern.description,
                            'match': match.group(0),
                            'context': context,
                            'replacement': re.sub(
                                pattern.sqlite_pattern,
                                pattern.postgresql_replacement,
                                match.group(0),
                                flags=pattern.flags
                            )
                        })
            except re.error as e:
                logger.warning(f"Regex error in pattern {pattern.name}: {e}")

        # Also check for import patterns
        lines = content.split('\n')
        for i, line in enumerate(lines):
            for imp_pattern in self.import_patterns:
                if imp_pattern.sqlite_import in line:
                    findings.append({
                        'file': file_path,
                        'line': i + 1,
                        'pattern': f"import_{imp_pattern.name}",
                        'description': imp_pattern.description,
                        'match': line,
                        'context': line,
                        'replacement': imp_pattern.postgresql_import
                    })

        return findings

    def convert_file(self, file_path: str, dry_run: bool = False, create_backup: bool = True) -> List[FileChange]:
        """Convert SQLite code to PostgreSQL in a file."""
        content = self.read_file(file_path)
        original_content = content
        changes = []

        # Apply pattern replacements
        for pattern in self.patterns:
            try:
                def replacement_func(match):
                    old_text = match.group(0)
                    new_text = re.sub(
                        pattern.sqlite_pattern,
                        pattern.postgresql_replacement,
                        old_text,
                        flags=pattern.flags
                    )

                    # Calculate line number
                    line_number = content[:match.start()].count('\n') + 1

                    changes.append(FileChange(
                        file_path=file_path,
                        line_number=line_number,
                        old_text=old_text,
                        new_text=new_text,
                        pattern_name=pattern.name
                    ))

                    return new_text

                content = re.sub(
                    pattern.sqlite_pattern,
                    replacement_func,
                    content,
                    flags=pattern.flags
                )
            except re.error as e:
                logger.warning(f"Regex error applying pattern {pattern.name}: {e}")

        # Apply import replacements
        lines = content.split('\n')
        for i, line in enumerate(lines):
            for imp_pattern in self.import_patterns:
                if imp_pattern.sqlite_import in line and imp_pattern.sqlite_import != "# PostgreSQL imports":
                    old_line = line
                    new_line = line.replace(imp_pattern.sqlite_import, imp_pattern.postgresql_import)

                    changes.append(FileChange(
                        file_path=file_path,
                        line_number=i + 1,
                        old_text=old_line,
                        new_text=new_line,
                        pattern_name=f"import_{imp_pattern.name}"
                    ))

                    lines[i] = new_line

        content = '\n'.join(lines)

        # Only write if changes were made and not in dry run mode
        if changes and not dry_run:
            if create_backup:
                self.backup_file(file_path)

            self.write_file(file_path, content)
            logger.info(f"Converted {file_path}: {len(changes)} changes")

        return changes

    def scan_directory(self, path: str) -> Dict[str, List[Dict[str, Any]]]:
        """Scan a directory for SQLite code patterns."""
        files = self.find_files(path)
        all_findings = {}

        logger.info(f"Scanning {len(files)} files in {path}")

        for file_path in files:
            findings = self.scan_file(file_path)
            if findings:
                all_findings[file_path] = findings

        return all_findings

    def convert_directory(self, path: str, dry_run: bool = False, create_backup: bool = True) -> Dict[str, List[FileChange]]:
        """Convert SQLite code to PostgreSQL in a directory."""
        files = self.find_files(path)
        all_changes = {}

        logger.info(f"Converting {len(files)} files in {path}")

        for file_path in files:
            changes = self.convert_file(file_path, dry_run, create_backup)
            if changes:
                all_changes[file_path] = changes

        return all_changes

    def generate_report(self, findings: Dict[str, List[Dict[str, Any]]]) -> str:
        """Generate a human-readable report of findings."""
        report_lines = []

        total_files = len(findings)
        total_patterns = sum(len(finds) for finds in findings.values())

        report_lines.append("=" * 80)
        report_lines.append("SQLite to PostgreSQL Code Migration Scan Report")
        report_lines.append("=" * 80)
        report_lines.append(f"Scan completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append(f"Files scanned: {total_files}")
        report_lines.append(f"Patterns found: {total_patterns}")
        report_lines.append("")

        if not findings:
            report_lines.append("No SQLite patterns found that need conversion.")
            return "\n".join(report_lines)

        # Group by pattern type
        pattern_counts = {}
        for file_findings in findings.values():
            for finding in file_findings:
                pattern_name = finding['pattern']
                pattern_counts[pattern_name] = pattern_counts.get(pattern_name, 0) + 1

        report_lines.append("Pattern Summary:")
        for pattern_name, count in sorted(pattern_counts.items()):
            report_lines.append(f"  {pattern_name}: {count}")

        report_lines.append("")
        report_lines.append("Detailed Findings:")
        report_lines.append("")

        for file_path, file_findings in findings.items():
            report_lines.append(f"File: {file_path}")
            report_lines.append(f"  Patterns found: {len(file_findings)}")

            for finding in file_findings:
                report_lines.append(f"  - Line {finding['line']}: {finding['pattern']}")
                report_lines.append(f"    Description: {finding['description']}")
                report_lines.append(f"    Match: {finding['match']}")
                report_lines.append(f"    Replacement: {finding['replacement']}")
                report_lines.append(f"    Context: ...{finding['context']}...")
                report_lines.append("")

        report_lines.append("=" * 80)

        return "\n".join(report_lines)

    def generate_conversion_summary(self, changes: Dict[str, List[FileChange]]) -> str:
        """Generate a summary of conversion changes."""
        summary_lines = []

        total_files = len(changes)
        total_changes = sum(len(file_changes) for file_changes in changes.values())

        summary_lines.append("=" * 80)
        summary_lines.append("SQLite to PostgreSQL Conversion Summary")
        summary_lines.append("=" * 80)
        summary_lines.append(f"Conversion completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        summary_lines.append(f"Files converted: {total_files}")
        summary_lines.append(f"Total changes: {total_changes}")
        summary_lines.append("")

        if not changes:
            summary_lines.append("No changes were made.")
            return "\n".join(summary_lines)

        # Group by pattern type
        pattern_counts = {}
        for file_changes in changes.values():
            for change in file_changes:
                pattern_name = change.pattern_name
                pattern_counts[pattern_name] = pattern_counts.get(pattern_name, 0) + 1

        summary_lines.append("Changes by Pattern Type:")
        for pattern_name, count in sorted(pattern_counts.items()):
            summary_lines.append(f"  {pattern_name}: {count}")

        summary_lines.append("")
        summary_lines.append("File Details:")
        summary_lines.append("")

        for file_path, file_changes in changes.items():
            summary_lines.append(f"File: {file_path}")
            summary_lines.append(f"  Changes: {len(file_changes)}")

            for change in file_changes[:5]:  # Show first 5 changes per file
                summary_lines.append(f"  - Line {change.line_number}: {change.pattern_name}")
                old_preview = change.old_text[:50] + ("..." if len(change.old_text) > 50 else "")
                new_preview = change.new_text[:50] + ("..." if len(change.new_text) > 50 else "")
                summary_lines.append(f"    Old: {old_preview}")
                summary_lines.append(f"    New: {new_preview}")

            if len(file_changes) > 5:
                summary_lines.append(f"    ... and {len(file_changes) - 5} more changes")

            summary_lines.append("")

        summary_lines.append("=" * 80)

        return "\n".join(summary_lines)

    def check_python_syntax(self, file_path: str) -> bool:
        """Check if Python file has valid syntax after conversion."""
        try:
            content = self.read_file(file_path)
            ast.parse(content)
            return True
        except SyntaxError as e:
            logger.error(f"Syntax error in {file_path}: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Convert SQLite code to PostgreSQL syntax for duo-moonshot"
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Scan command
    scan_parser = subparsers.add_parser('scan', help='Scan files for SQLite patterns')
    scan_parser.add_argument('path', help='File or directory to scan')
    scan_parser.add_argument('--output', '-o', help='Output report file')

    # Convert command
    convert_parser = subparsers.add_parser('convert', help='Convert files from SQLite to PostgreSQL')
    convert_parser.add_argument('path', help='File or directory to convert')
    convert_parser.add_argument('--dry-run', '-d', action='store_true',
                               help='Dry run without making changes')
    convert_parser.add_argument('--no-backup', action='store_true',
                               help='Do not create backup files')
    convert_parser.add_argument('--output', '-o', help='Output summary file')

    # Test command
    test_parser = subparsers.add_parser('test', help='Test conversion on sample code')
    test_parser.add_argument('--code', help='Test code snippet')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    converter = SQLiteToPostgreSQLConverter()

    if args.command == 'scan':
        print(f"Scanning {args.path} for SQLite patterns...")
        findings = converter.scan_directory(args.path)

        report = converter.generate_report(findings)
        print(report)

        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(report)
            print(f"\nReport saved to: {args.output}")

        # Exit with code based on findings
        if findings:
            print(f"\nFound {sum(len(f) for f in findings.values())} patterns that need conversion.")
            return 0
        else:
            print("\nNo SQLite patterns found that need conversion.")
            return 0

    elif args.command == 'convert':
        if args.dry_run:
            print(f"Dry run: Checking what would be converted in {args.path}...")
            # For dry run, just scan and show what would be changed
            findings = converter.scan_directory(args.path)

            report = converter.generate_report(findings)
            print(report)

            if args.output:
                with open(args.output, 'w', encoding='utf-8') as f:
                    f.write(report)
                print(f"\nDry run report saved to: {args.output}")

            if findings:
                print("\nThis was a dry run. No changes were made.")
                print("To apply conversions, run without --dry-run flag.")

            return 0
        else:
            print(f"Converting {args.path} from SQLite to PostgreSQL...")

            # Ask for confirmation if converting a directory
            path_obj = Path(args.path)
            if path_obj.is_dir():
                confirm = input(f"Are you sure you want to convert all Python files in {args.path}? (y/N): ")
                if confirm.lower() != 'y':
                    print("Conversion cancelled.")
                    return 0

            create_backup = not args.no_backup
            changes = converter.convert_directory(args.path, dry_run=False, create_backup=create_backup)

            summary = converter.generate_conversion_summary(changes)
            print(summary)

            if args.output:
                with open(args.output, 'w', encoding='utf-8') as f:
                    f.write(summary)
                print(f"\nConversion summary saved to: {args.output}")

            # Verify syntax of converted files
            print("\nVerifying syntax of converted files...")
            syntax_errors = []
            for file_path in changes.keys():
                if not converter.check_python_syntax(file_path):
                    syntax_errors.append(file_path)

            if syntax_errors:
                print(f"WARNING: Syntax errors found in {len(syntax_errors)} files:")
                for file_path in syntax_errors:
                    print(f"  - {file_path}")
                print("\nPlease review the converted files and restore from backup if needed.")
                return 1
            else:
                print("All converted files have valid Python syntax.")
                return 0

    elif args.command == 'test':
        if args.code:
            test_code = args.code
        else:
            # Example SQLite code to test
            test_code = """
import sqlite3

conn = sqlite3.connect('crypto_data.db')
cursor = conn.cursor()

# Check if table exists
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))

# Query with placeholders
cursor.execute('''
    SELECT close FROM Kline5m_BTCUSDT
    WHERE open_time = ? AND close > ?
''', (target_time, min_price))

# Dynamic table name
table_name = f'Kline5m_{symbol}'
cursor.execute(f"SELECT * FROM {table_name} WHERE open_time = ?", (timestamp,))

# Funding rate query
cursor.execute('''
    SELECT funding_time, funding_rate
    FROM funding_rate_history
    WHERE symbol = ?
      AND funding_time >= ?
      AND funding_time < ?
''', (symbol, entry_time, exit_time))
"""

        print("Testing conversion on sample code:")
        print("=" * 80)
        print("Original SQLite code:")
        print(test_code)
        print("=" * 80)

        # Create a temporary converter
        converter = SQLiteToPostgreSQLConverter()

        # Apply conversions
        converted_code = test_code
        for pattern in converter.patterns:
            try:
                converted_code = re.sub(
                    pattern.sqlite_pattern,
                    pattern.postgresql_replacement,
                    converted_code,
                    flags=pattern.flags
                )
            except re.error:
                pass

        print("Converted PostgreSQL code:")
        print(converted_code)
        print("=" * 80)

        return 0

    return 0


if __name__ == '__main__':
    sys.exit(main())
