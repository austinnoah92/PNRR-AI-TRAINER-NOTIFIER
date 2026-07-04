from __future__ import annotations

import sqlite3
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock

from .models import AlboItem, Opportunity

# Columns update_thread() is allowed to touch — whitelisted so the dynamic
# UPDATE builder never interpolates caller-controlled column names.
_THREAD_UPDATABLE_FIELDS = frozenset({
    "role_signature", "protocol_ref", "status", "subject", "message_id", "last_item_key", "last_title",
})

STATUS_OPEN = "aperta"
STATUS_EXPIRED = "scaduta"
STATUS_UNKNOWN = "scadenza sconosciuta"


def classify_status(deadline: str, today: date | None = None) -> str:
    today = today or date.today()
    raw = (deadline or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            due = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        return STATUS_EXPIRED if due < today else STATUS_OPEN
    return STATUS_UNKNOWN


class StateStore(ABC):
    @abstractmethod
    def unprocessed_keys(self, keys: Iterable[str]) -> set[str]: ...

    @abstractmethod
    def mark_processed(self, items: Iterable[AlboItem]) -> None: ...

    @abstractmethod
    def has_alerted(self, alert_key: str) -> bool: ...

    @abstractmethod
    def mark_alerted(self, alert_key: str, school_code: str, url: str) -> None: ...

    @abstractmethod
    def record_opportunity(self, opportunity: Opportunity) -> None: ...

    @abstractmethod
    def iter_opportunities(self) -> list[dict]: ...

    @abstractmethod
    def record_decisions(self, decisions: Iterable[tuple[str, str, str, str, str, str]]) -> None:
        """Bulk-upsert (item_key, school_code, title, url, stage, reason) rows.

        Upserted by item_key (not appended) so this stays a live "current status
        of every notice we've ever seen", not a growing per-run log — it scales
        to the full funded list run hourly without bloating. This is what makes
        "why didn't school X alert" answerable generically, for ANY school and
        ANY reason (prefilter miss, rule rejection, AI rejection, superseded,
        expired, already-alerted...), not just today's known bug.
        """

    @abstractmethod
    def iter_decisions(self, school_code: str | None = None) -> list[dict]: ...

    @abstractmethod
    def get_checkpoint(self) -> int:
        """Index into the mapped-schools list where the next run should start."""

    @abstractmethod
    def advance_checkpoint(self, count: int, total: int) -> None:
        """Move the checkpoint forward by `count` schools, wrapping at `total`."""

    @abstractmethod
    def find_open_threads(self, school_code: str, cup: str, since_days: int = 60) -> list[dict]:
        """held/sent call_threads rows for this school+CUP, updated within since_days."""

    @abstractmethod
    def create_thread(
        self, school_code: str, cup: str, clp: str, role_signature: str, protocol_ref: str | None,
        status: str, subject: str | None, message_id: str | None, last_item_key: str, last_title: str = "",
    ) -> str:
        """Create a new call_threads row and return its generated thread_id."""

    @abstractmethod
    def update_thread(self, thread_id: str, **fields: str | None) -> None:
        """Update one or more of _THREAD_UPDATABLE_FIELDS on an existing thread."""

    def close(self) -> None:
        pass

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS processed_items (
    item_key    TEXT PRIMARY KEY,
    school_code TEXT NOT NULL,
    title       TEXT,
    published   TEXT,
    first_seen  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_processed_school ON processed_items(school_code);

CREATE TABLE IF NOT EXISTS alerts (
    alert_key   TEXT PRIMARY KEY,
    school_code TEXT NOT NULL,
    url         TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunities (
    opp_key          TEXT PRIMARY KEY,
    school_code      TEXT NOT NULL,
    school_name      TEXT,
    region           TEXT,
    cup              TEXT,
    clp              TEXT,
    title            TEXT,
    url              TEXT,
    published        TEXT,
    deadline         TEXT,
    opportunity_type TEXT,
    confidence       TEXT,
    first_seen       TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen        TEXT DEFAULT CURRENT_TIMESTAMP
);

-- The decision trail: what happened to every notice that reached evaluation
-- (i.e. survived the prefilter... plus the prefilter rejections themselves, so
-- a vocabulary gap is visible too), and why. Keyed by item_key so re-runs
-- update a notice's row in place rather than accumulating duplicates.
CREATE TABLE IF NOT EXISTS decisions (
    item_key    TEXT PRIMARY KEY,
    school_code TEXT NOT NULL,
    title       TEXT,
    url         TEXT,
    stage       TEXT NOT NULL,
    reason      TEXT,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_decisions_school ON decisions(school_code);

-- Rotating cursor into the mapped-schools list: a single row tracking where
-- the NEXT run should start, so a run that gets cut off partway (timeout,
-- manual cancel) doesn't cause the same head of the list to be re-polled
-- forever while the tail is never reached.
CREATE TABLE IF NOT EXISTS checkpoint (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    next_index  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- One row per distinct selection process (not per document). A "Decreto di
-- avvio" and its companion "Avviso pubblico" both cite the same process but
-- arrive as separate documents/URLs — this is what lets the monitor recognize
-- them as one thread instead of two unrelated alerts, without conflating
-- genuinely different processes that happen to share a CUP.
CREATE TABLE IF NOT EXISTS call_threads (
    thread_id      TEXT PRIMARY KEY,
    school_code    TEXT NOT NULL,
    cup            TEXT,
    clp            TEXT,
    role_signature TEXT,
    protocol_ref   TEXT,
    status         TEXT NOT NULL,
    subject        TEXT,
    message_id     TEXT,
    last_item_key  TEXT,
    last_title     TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_call_threads_school_cup ON call_threads(school_code, cup);
"""


class SqliteStateStore(StateStore):
    def __init__(self, db_path: Path | str) -> None:
        self._lock = RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQLITE)
        self._conn.commit()

    def unprocessed_keys(self, keys: Iterable[str]) -> set[str]:
        wanted = set(keys)
        if not wanted:
            return set()
        known: set[str] = set()
        bucket = list(wanted)
        with self._lock:
            for start in range(0, len(bucket), 500):
                chunk = bucket[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT item_key FROM processed_items WHERE item_key IN ({placeholders})",
                    chunk,
                )
                known.update(row[0] for row in rows)
        return wanted - known

    def mark_processed(self, items: Iterable[AlboItem]) -> None:
        payload = [(it.key, it.school_code, it.title, it.published) for it in items]
        if not payload:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO processed_items (item_key, school_code, title, published) VALUES (?, ?, ?, ?)",
                payload,
            )
            self._conn.commit()

    def has_alerted(self, alert_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM alerts WHERE alert_key = ? LIMIT 1", (alert_key,))
            return cur.fetchone() is not None

    def mark_alerted(self, alert_key: str, school_code: str, url: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO alerts (alert_key, school_code, url) VALUES (?, ?, ?)",
                (alert_key, school_code, url),
            )
            self._conn.commit()

    def record_opportunity(self, o: Opportunity) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO opportunities
                    (opp_key, school_code, school_name, region, cup, clp, title, url,
                     published, deadline, opportunity_type, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opp_key) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    deadline = excluded.deadline,
                    opportunity_type = excluded.opportunity_type,
                    confidence = excluded.confidence
                """,
                (o.key, o.school_code, o.school_name, o.region, o.cup, o.clp, o.title, o.url,
                 o.published, o.deadline, o.opportunity_type, o.confidence),
            )
            self._conn.commit()

    def iter_opportunities(self) -> list[dict]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute("SELECT * FROM opportunities ORDER BY first_seen DESC").fetchall()
            self._conn.row_factory = None
        return [dict(r) for r in rows]

    def record_decisions(self, decisions: Iterable[tuple[str, str, str, str, str, str]]) -> None:
        payload = list(decisions)
        if not payload:
            return
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO decisions (item_key, school_code, title, url, stage, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    title = excluded.title, url = excluded.url,
                    stage = excluded.stage, reason = excluded.reason,
                    updated_at = CURRENT_TIMESTAMP
                """,
                payload,
            )
            self._conn.commit()

    def iter_decisions(self, school_code: str | None = None) -> list[dict]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            if school_code:
                rows = self._conn.execute(
                    "SELECT * FROM decisions WHERE school_code = ? ORDER BY updated_at DESC", (school_code,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM decisions ORDER BY updated_at DESC").fetchall()
            self._conn.row_factory = None
        return [dict(r) for r in rows]

    def get_checkpoint(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT next_index FROM checkpoint WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row else 0

    def advance_checkpoint(self, count: int, total: int) -> None:
        if total <= 0:
            return
        with self._lock:
            cur = self._conn.execute("SELECT next_index FROM checkpoint WHERE id = 1")
            row = cur.fetchone()
            current = row[0] if row else 0
            new_index = (current + count) % total
            self._conn.execute(
                """
                INSERT INTO checkpoint (id, next_index) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET next_index = excluded.next_index,
                                               updated_at = CURRENT_TIMESTAMP
                """,
                (new_index,),
            )
            self._conn.commit()

    def find_open_threads(self, school_code: str, cup: str, since_days: int = 60) -> list[dict]:
        cutoff = (datetime.utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(
                """
                SELECT * FROM call_threads
                WHERE school_code = ? AND cup = ? AND status IN ('held', 'sent') AND updated_at >= ?
                ORDER BY updated_at DESC
                """,
                (school_code, cup, cutoff),
            ).fetchall()
            self._conn.row_factory = None
        return [dict(r) for r in rows]

    def create_thread(
        self, school_code: str, cup: str, clp: str, role_signature: str, protocol_ref: str | None,
        status: str, subject: str | None, message_id: str | None, last_item_key: str, last_title: str = "",
    ) -> str:
        thread_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO call_threads
                    (thread_id, school_code, cup, clp, role_signature, protocol_ref, status,
                     subject, message_id, last_item_key, last_title)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (thread_id, school_code, cup, clp, role_signature, protocol_ref, status,
                 subject, message_id, last_item_key, last_title),
            )
            self._conn.commit()
        return thread_id

    def update_thread(self, thread_id: str, **fields: str | None) -> None:
        columns = [c for c in fields if c in _THREAD_UPDATABLE_FIELDS]
        if not columns:
            return
        assignments = ", ".join(f"{c} = ?" for c in columns)
        with self._lock:
            self._conn.execute(
                f"UPDATE call_threads SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE thread_id = ?",
                [fields[c] for c in columns] + [thread_id],
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.close()


class PostgresStateStore(StateStore):
    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as exc:
            raise RuntimeError("Postgres persistence requires the 'psycopg[binary]' dependency.") from exc
        self._lock = RLock()
        self._conn = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_items (
                    item_key    TEXT PRIMARY KEY,
                    school_code TEXT NOT NULL,
                    title       TEXT,
                    published   TEXT,
                    first_seen  TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_processed_school ON processed_items(school_code);

                CREATE TABLE IF NOT EXISTS alerts (
                    alert_key   TEXT PRIMARY KEY,
                    school_code TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    created_at  TIMESTAMPTZ DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS opportunities (
                    opp_key          TEXT PRIMARY KEY,
                    school_code      TEXT NOT NULL,
                    school_name      TEXT,
                    region           TEXT,
                    cup              TEXT,
                    clp              TEXT,
                    title            TEXT,
                    url              TEXT,
                    published        TEXT,
                    deadline         TEXT,
                    opportunity_type TEXT,
                    confidence       TEXT,
                    first_seen       TIMESTAMPTZ DEFAULT now(),
                    last_seen        TIMESTAMPTZ DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    item_key    TEXT PRIMARY KEY,
                    school_code TEXT NOT NULL,
                    title       TEXT,
                    url         TEXT,
                    stage       TEXT NOT NULL,
                    reason      TEXT,
                    updated_at  TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_school ON decisions(school_code);

                CREATE TABLE IF NOT EXISTS checkpoint (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    next_index  INTEGER NOT NULL DEFAULT 0,
                    updated_at  TIMESTAMPTZ DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS call_threads (
                    thread_id      TEXT PRIMARY KEY,
                    school_code    TEXT NOT NULL,
                    cup            TEXT,
                    clp            TEXT,
                    role_signature TEXT,
                    protocol_ref   TEXT,
                    status         TEXT NOT NULL,
                    subject        TEXT,
                    message_id     TEXT,
                    last_item_key  TEXT,
    last_title     TEXT,
                    created_at     TIMESTAMPTZ DEFAULT now(),
                    updated_at     TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_call_threads_school_cup ON call_threads(school_code, cup);
                """
            )

    def unprocessed_keys(self, keys: Iterable[str]) -> set[str]:
        wanted = set(keys)
        if not wanted:
            return set()
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT item_key FROM processed_items WHERE item_key = ANY(%s)", (list(wanted),))
            known = {row["item_key"] for row in cur.fetchall()}
        return wanted - known

    def mark_processed(self, items: Iterable[AlboItem]) -> None:
        payload = [(it.key, it.school_code, it.title, it.published) for it in items]
        if not payload:
            return
        with self._lock, self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO processed_items (item_key, school_code, title, published)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (item_key) DO NOTHING
                """,
                payload,
            )

    def has_alerted(self, alert_key: str) -> bool:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM alerts WHERE alert_key = %s LIMIT 1", (alert_key,))
            return cur.fetchone() is not None

    def mark_alerted(self, alert_key: str, school_code: str, url: str) -> None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alerts (alert_key, school_code, url)
                VALUES (%s, %s, %s)
                ON CONFLICT (alert_key) DO NOTHING
                """,
                (alert_key, school_code, url),
            )

    def record_opportunity(self, o: Opportunity) -> None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO opportunities
                    (opp_key, school_code, school_name, region, cup, clp, title, url,
                     published, deadline, opportunity_type, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (opp_key) DO UPDATE SET
                    last_seen = now(),
                    deadline = EXCLUDED.deadline,
                    opportunity_type = EXCLUDED.opportunity_type,
                    confidence = EXCLUDED.confidence
                """,
                (o.key, o.school_code, o.school_name, o.region, o.cup, o.clp, o.title, o.url,
                 o.published, o.deadline, o.opportunity_type, o.confidence),
            )

    def iter_opportunities(self) -> list[dict]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT * FROM opportunities ORDER BY first_seen DESC")
            return [dict(row) for row in cur.fetchall()]

    def record_decisions(self, decisions: Iterable[tuple[str, str, str, str, str, str]]) -> None:
        payload = list(decisions)
        if not payload:
            return
        with self._lock, self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO decisions (item_key, school_code, title, url, stage, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (item_key) DO UPDATE SET
                    title = EXCLUDED.title, url = EXCLUDED.url,
                    stage = EXCLUDED.stage, reason = EXCLUDED.reason,
                    updated_at = now()
                """,
                payload,
            )

    def iter_decisions(self, school_code: str | None = None) -> list[dict]:
        with self._lock, self._conn.cursor() as cur:
            if school_code:
                cur.execute("SELECT * FROM decisions WHERE school_code = %s ORDER BY updated_at DESC", (school_code,))
            else:
                cur.execute("SELECT * FROM decisions ORDER BY updated_at DESC")
            return [dict(row) for row in cur.fetchall()]

    def get_checkpoint(self) -> int:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT next_index FROM checkpoint WHERE id = 1")
            row = cur.fetchone()
            return row["next_index"] if row else 0

    def advance_checkpoint(self, count: int, total: int) -> None:
        if total <= 0:
            return
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT next_index FROM checkpoint WHERE id = 1")
            row = cur.fetchone()
            current = row["next_index"] if row else 0
            new_index = (current + count) % total
            cur.execute(
                """
                INSERT INTO checkpoint (id, next_index) VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET next_index = EXCLUDED.next_index,
                                                updated_at = now()
                """,
                (new_index,),
            )

    def find_open_threads(self, school_code: str, cup: str, since_days: int = 60) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(days=since_days)
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM call_threads
                WHERE school_code = %s AND cup = %s AND status IN ('held', 'sent') AND updated_at >= %s
                ORDER BY updated_at DESC
                """,
                (school_code, cup, cutoff),
            )
            return [dict(row) for row in cur.fetchall()]

    def create_thread(
        self, school_code: str, cup: str, clp: str, role_signature: str, protocol_ref: str | None,
        status: str, subject: str | None, message_id: str | None, last_item_key: str, last_title: str = "",
    ) -> str:
        thread_id = uuid.uuid4().hex
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO call_threads
                    (thread_id, school_code, cup, clp, role_signature, protocol_ref, status,
                     subject, message_id, last_item_key, last_title)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (thread_id, school_code, cup, clp, role_signature, protocol_ref, status,
                 subject, message_id, last_item_key, last_title),
            )
        return thread_id

    def update_thread(self, thread_id: str, **fields: str | None) -> None:
        columns = [c for c in fields if c in _THREAD_UPDATABLE_FIELDS]
        if not columns:
            return
        assignments = ", ".join(f"{c} = %s" for c in columns)
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE call_threads SET {assignments}, updated_at = now() WHERE thread_id = %s",
                [fields[c] for c in columns] + [thread_id],
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def build_state_store(database_url: str | None, sqlite_path: Path | str) -> StateStore:
    if database_url:
        return PostgresStateStore(database_url)
    return SqliteStateStore(sqlite_path)
