"""SQLite persistence: people, face embeddings and detection events."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS embeddings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    vector      BLOB NOT NULL,           -- float32, L2-normalised
    photo_path  TEXT,                    -- relative to DATA_DIR
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     INTEGER REFERENCES people(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    confidence    REAL NOT NULL,
    snapshot_path TEXT,                  -- relative to DATA_DIR
    camera        TEXT,
    timestamp     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_embeddings_person ON embeddings(person_id);
"""


class Database:
    """Tiny thread-safe wrapper. SQLite is plenty for a few cameras."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- people ---------------------------------------------------------
    def get_or_create_person(self, name: str) -> int:
        rows = self._query("SELECT id FROM people WHERE name = ? COLLATE NOCASE", (name,))
        if rows:
            return rows[0]["id"]
        return self._exec(
            "INSERT INTO people (name, created_at) VALUES (?, ?)", (name, time.time())
        ).lastrowid

    def list_people(self) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT p.id, p.name, p.created_at,
                   COUNT(e.id) AS photo_count,
                   (SELECT photo_path FROM embeddings WHERE person_id = p.id
                     ORDER BY id LIMIT 1) AS photo_path,
                   (SELECT MAX(timestamp) FROM events WHERE person_id = p.id) AS last_seen
            FROM people p LEFT JOIN embeddings e ON e.person_id = p.id
            GROUP BY p.id ORDER BY p.name COLLATE NOCASE
            """
        )

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM people WHERE id = ?", (person_id,))
        return rows[0] if rows else None

    def delete_person(self, person_id: int) -> list[str]:
        """Delete a person; returns the photo paths so the caller can remove files."""
        paths = [
            r["photo_path"]
            for r in self._query("SELECT photo_path FROM embeddings WHERE person_id = ?", (person_id,))
            if r["photo_path"]
        ]
        self._exec("DELETE FROM people WHERE id = ?", (person_id,))
        return paths

    # ---- embeddings -----------------------------------------------------
    def add_embedding(self, person_id: int, vector: np.ndarray, photo_path: str | None) -> int:
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        return self._exec(
            "INSERT INTO embeddings (person_id, vector, photo_path, created_at) VALUES (?, ?, ?, ?)",
            (person_id, blob, photo_path, time.time()),
        ).lastrowid

    def all_embeddings(self) -> list[tuple[int, str, np.ndarray]]:
        rows = self._query(
            "SELECT e.person_id, p.name, e.vector FROM embeddings e JOIN people p ON p.id = e.person_id"
        )
        return [(r["person_id"], r["name"], np.frombuffer(r["vector"], dtype=np.float32)) for r in rows]

    # ---- events ---------------------------------------------------------
    def add_event(
        self,
        person_id: int | None,
        name: str,
        confidence: float,
        snapshot_path: str | None,
        camera: str,
        timestamp: float,
    ) -> dict[str, Any]:
        event_id = self._exec(
            """INSERT INTO events (person_id, name, confidence, snapshot_path, camera, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (person_id, name, confidence, snapshot_path, camera, timestamp),
        ).lastrowid
        return {
            "id": event_id,
            "person_id": person_id,
            "name": name,
            "confidence": confidence,
            "snapshot_path": snapshot_path,
            "camera": camera,
            "timestamp": timestamp,
        }

    def list_events(self, limit: int = 100, offset: int = 0, name: str | None = None) -> list[dict[str, Any]]:
        if name:
            return self._query(
                "SELECT * FROM events WHERE name LIKE ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (f"%{name}%", limit, offset),
            )
        return self._query(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    def clear_events(self) -> list[str]:
        paths = [r["snapshot_path"] for r in self._query("SELECT snapshot_path FROM events") if r["snapshot_path"]]
        self._exec("DELETE FROM events")
        return paths
