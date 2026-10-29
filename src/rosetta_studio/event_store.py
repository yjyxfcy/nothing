"""Local event history shared by Rosetta MCP processes and the Studio GUI."""
from __future__ import annotations

import sqlite3
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path(os.environ.get("ROSETTA_EVENTS_DB", str(Path.home() / ".rosetta" / "events.sqlite3")))
SNAPSHOT_LIMIT = 200_000


class EventStore:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    session_id TEXT,
                    pair_name TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_path TEXT,
                    agent_view TEXT,
                    target_view TEXT,
                    detail TEXT NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _snapshot(value: str | None) -> str | None:
        if value is None or len(value) <= SNAPSHOT_LIMIT:
            return value
        return value[:SNAPSHOT_LIMIT] + "\n\n[View truncated in activity history]"

    def record(self, *, source: str, session_id: str | None, pair_name: str | None,
               action: str, status: str, target_path: str | None,
               agent_view: str | None, target_view: str | None, detail: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("""
                INSERT INTO events
                    (created_at, source, session_id, pair_name, action, status,
                     target_path, agent_view, target_view, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                source, session_id, pair_name, action, status, target_path,
                self._snapshot(agent_view), self._snapshot(target_view), detail,
            ))

    def recent(self, limit: int = 100) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, created_at, source, session_id, pair_name, action, status, "
                "target_path, detail FROM events ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_activity(self, limit: int = 100) -> tuple[int, list[dict]]:
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            latest_id = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            rows = db.execute(
                "SELECT id, created_at, source, session_id, pair_name, action, status, "
                "target_path, detail FROM events "
                "WHERE action != 'list_pairs' AND id <= ? ORDER BY id DESC LIMIT ?",
                (latest_id, limit),
            ).fetchall()
            db.commit()
        return latest_id, [dict(row) for row in reversed(rows)]

    def activity_since(self, event_id: int) -> tuple[int, list[dict]]:
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            latest_id = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            rows = db.execute(
                "SELECT id, created_at, source, session_id, pair_name, action, status, "
                "target_path, detail FROM events "
                "WHERE id > ? AND id <= ? AND action != 'list_pairs' ORDER BY id",
                (event_id, latest_id),
            ).fetchall()
            db.commit()
        return latest_id, [dict(row) for row in rows]

    def since(self, event_id: int, limit: int = 100) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, created_at, source, session_id, pair_name, action, status, "
                "target_path, detail FROM events WHERE id > ? ORDER BY id LIMIT ?",
                (event_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, event_id: int) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None
