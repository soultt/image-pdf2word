"""Conversion history backed by SQLite, with an in-memory fallback."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from .app_paths import get_history_path

PathLike = Union[str, Path]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class HistoryDB:
    """Stores one row per conversion attempt."""

    def __init__(self, path: Optional[PathLike] = None):
        self.path = Path(path) if path is not None else get_history_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    input_path TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER,
                    error TEXT,
                    params_json TEXT
                )
                """
            )

    def add_conversion(self, input_path: PathLike, output_path: PathLike, params: Dict) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO conversions "
                "(started_at, input_path, output_path, status, params_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _now(),
                    str(input_path),
                    str(output_path),
                    "running",
                    json.dumps(params, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def finish_conversion(
        self,
        record_id: int,
        status: str,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversions SET finished_at=?, status=?, duration_ms=?, error=? "
                "WHERE id=?",
                (_now(), status, duration_ms, error, record_id),
            )

    def list_conversions(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self, query: str = "", status: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        values: List[Any] = []
        if query:
            like = f"%{query}%"
            clauses.append("(input_path LIKE ? OR output_path LIKE ? OR error LIKE ?)")
            values.extend([like, like, like])
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM conversions{where} ORDER BY id DESC LIMIT ?", values
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversions WHERE id=?", (record_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def delete(self, record_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM conversions WHERE id=?", (record_id,))

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM conversions")


class MemoryHistory:
    """Fallback used when SQLite is unavailable; same interface as HistoryDB."""

    def __init__(self):
        self._rows: List[Dict[str, Any]] = []
        self._next_id = 1

    def add_conversion(self, input_path: PathLike, output_path: PathLike, params: Dict) -> int:
        record_id = self._next_id
        self._next_id += 1
        self._rows.append(
            {
                "id": record_id,
                "started_at": _now(),
                "finished_at": None,
                "input_path": str(input_path),
                "output_path": str(output_path),
                "status": "running",
                "duration_ms": None,
                "error": None,
                "params_json": json.dumps(params, ensure_ascii=False),
            }
        )
        return record_id

    def finish_conversion(
        self,
        record_id: int,
        status: str,
        duration_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        for row in self._rows:
            if row["id"] == record_id:
                row.update(
                    finished_at=_now(), status=status, duration_ms=duration_ms, error=error
                )
                return

    def list_conversions(self, limit: int = 200) -> List[Dict[str, Any]]:
        return list(reversed(self._rows))[:limit]

    def search(
        self, query: str = "", status: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
        rows = self.list_conversions(limit)
        query = (query or "").lower()
        return [
            row
            for row in rows
            if (not status or row["status"] == status)
            and (
                not query
                or query in row["input_path"].lower()
                or query in row["output_path"].lower()
                or query in (row["error"] or "").lower()
            )
        ]

    def get(self, record_id: int) -> Optional[Dict[str, Any]]:
        for row in self._rows:
            if row["id"] == record_id:
                return dict(row)
        return None

    def delete(self, record_id: int) -> None:
        self._rows = [row for row in self._rows if row["id"] != record_id]

    def clear(self) -> None:
        self._rows.clear()
