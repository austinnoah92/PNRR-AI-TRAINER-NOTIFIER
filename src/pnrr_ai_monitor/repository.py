from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .models import Alert, ProjectRecord


class ProjectRepository:
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path

    def load(self) -> list[ProjectRecord]:
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = csv.DictReader(handle)
            return [
                ProjectRecord(
                    progressive=(row.get("progressive") or "").strip(),
                    area=(row.get("area") or "").strip(),
                    school_code=(row.get("school_code") or "").strip(),
                    school_name=(row.get("school_name") or "").strip(),
                    region=(row.get("region") or "").strip(),
                    school_type=(row.get("school_type") or "").strip(),
                    cup=(row.get("cup") or "").strip(),
                    clp=(row.get("clp") or "").strip(),
                    amount=(row.get("amount") or "").strip(),
                    website=(row.get("website") or "").strip().rstrip("/"),
                    funded_date=(row.get("funded_date") or "").strip(),
                    vendor=(row.get("vendor") or "").strip(),
                    albo_id=(row.get("albo_id") or "").strip(),
                    albo_url=(row.get("albo_url") or "").strip().rstrip("/"),
                )
                for row in rows
                if (row.get("school_code") or "").strip()
            ]


class AlertHistory:
    def __init__(self, db_path: Path):
        self.connection = sqlite3.connect(db_path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_alerts (
                id TEXT PRIMARY KEY,
                school_code TEXT NOT NULL,
                cup TEXT NOT NULL,
                clp TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def has_seen(self, alert: Alert) -> bool:
        cursor = self.connection.execute("SELECT 1 FROM sent_alerts WHERE id = ?", (alert.unique_id,))
        return cursor.fetchone() is not None

    def save(self, alert: Alert) -> None:
        project = alert.candidate.project
        self.connection.execute(
            """
            INSERT OR IGNORE INTO sent_alerts (id, school_code, cup, clp, url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (alert.unique_id, project.school_code, project.cup, project.clp, alert.candidate.url),
        )
        self.connection.commit()
