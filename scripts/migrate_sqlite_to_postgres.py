"""Copy dedup state from the local SQLite fallback into Postgres.

One-off migration for the cutover to PostgresStateStore: reads every row out
of monitor_history.sqlite3 and inserts it into the target Postgres database,
using the same ON CONFLICT DO NOTHING pattern PostgresStateStore itself uses,
so the script is safe to re-run without duplicating anything. Run this once
against the real DATABASE_URL before adding it as a GitHub secret, so nothing
already alerted gets re-sent on cutover.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pnrr_ai_monitor.state import PostgresStateStore

_TABLES = {
    "processed_items": ("item_key", "school_code", "title", "published"),
    "alerts": ("alert_key", "school_code", "url"),
    "opportunities": (
        "opp_key", "school_code", "school_name", "region", "cup", "clp", "title", "url",
        "published", "deadline", "opportunity_type", "confidence",
    ),
    "decisions": ("item_key", "school_code", "title", "url", "stage", "reason"),
}


def migrate(sqlite_path: Path, database_url: str) -> None:
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = PostgresStateStore(database_url)

    for table, columns in _TABLES.items():
        rows = src.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
        payload = [tuple(row[c] for c in columns) for row in rows]
        if payload:
            placeholders = ", ".join(["%s"] * len(columns))
            with dst._conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                    f"ON CONFLICT DO NOTHING",
                    payload,
                )
        src_count = len(rows)
        with dst._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            dst_count = cur.fetchone()["n"]
        print(f"{table}: {src_count} rows in SQLite -> {dst_count} rows now in Postgres")

    src.close()
    dst.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate monitor_history.sqlite3 dedup state into Postgres.")
    parser.add_argument("--sqlite-path", default=str(ROOT / "monitor_history.sqlite3"),
                         help="Path to the SQLite state file (default: monitor_history.sqlite3).")
    parser.add_argument("--database-url", default=None,
                         help="Postgres connection string. Defaults to the DATABASE_URL env var.")
    args = parser.parse_args()

    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("--database-url not given and DATABASE_URL is not set in the environment.")

    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        parser.error(f"SQLite file not found: {sqlite_path}")

    migrate(sqlite_path, database_url)


if __name__ == "__main__":
    main()
