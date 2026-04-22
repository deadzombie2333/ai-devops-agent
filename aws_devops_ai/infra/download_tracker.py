"""Download tracker — SQLite-backed persistence for log download deduplication."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from aws_devops_ai.models import DownloadRecord, LogReference


class DownloadTracker:
    """Persists download state in SQLite to prevent re-downloading logs."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                unique_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_identifier TEXT NOT NULL,
                key TEXT NOT NULL,
                local_path TEXT,
                downloaded_at TEXT NOT NULL,
                is_purged INTEGER NOT NULL DEFAULT 0,
                purged_at TEXT
            )
        """)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def is_downloaded(self, log_ref: LogReference) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM downloads WHERE unique_id = ?", (log_ref.unique_id,)
        ).fetchone()
        return row is not None

    def mark_downloaded(
        self,
        log_ref: LogReference,
        local_path: str,
        downloaded_at: datetime | None = None,
    ) -> None:
        """Record a download. Idempotent — INSERT OR IGNORE."""
        ts = downloaded_at or datetime.utcnow()
        self._conn.execute(
            """INSERT OR IGNORE INTO downloads
               (unique_id, source_type, source_identifier, key, local_path, downloaded_at, is_purged)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (
                log_ref.unique_id,
                log_ref.source.source_type.value,
                log_ref.source.identifier,
                log_ref.key,
                local_path,
                ts.isoformat(),
            ),
        )
        self._conn.commit()

    def mark_purged(self, unique_id: str) -> None:
        """Soft-delete: set is_purged, clear local_path, set purged_at."""
        now = datetime.utcnow().isoformat()
        self._conn.execute(
            """UPDATE downloads
               SET is_purged = 1, local_path = NULL, purged_at = ?
               WHERE unique_id = ?""",
            (now, unique_id),
        )
        self._conn.commit()

    def is_purged(self, unique_id: str) -> bool:
        row = self._conn.execute(
            "SELECT is_purged FROM downloads WHERE unique_id = ?", (unique_id,)
        ).fetchone()
        return bool(row and row["is_purged"])

    def get_record(self, unique_id: str) -> DownloadRecord | None:
        row = self._conn.execute(
            "SELECT * FROM downloads WHERE unique_id = ?", (unique_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_downloaded_since(self, since: datetime) -> list[DownloadRecord]:
        rows = self._conn.execute(
            "SELECT * FROM downloads WHERE downloaded_at >= ?",
            (since.isoformat(),),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_all_downloaded(self) -> set[str]:
        rows = self._conn.execute("SELECT unique_id FROM downloads").fetchall()
        return {r["unique_id"] for r in rows}

    def get_expired_records(self, before: datetime) -> list[DownloadRecord]:
        rows = self._conn.execute(
            "SELECT * FROM downloads WHERE downloaded_at < ? AND is_purged = 0",
            (before.isoformat(),),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def restore_record(self, unique_id: str, new_local_path: str) -> None:
        """Restore a purged record after re-download."""
        now = datetime.utcnow().isoformat()
        self._conn.execute(
            """UPDATE downloads
               SET is_purged = 0, local_path = ?, downloaded_at = ?, purged_at = NULL
               WHERE unique_id = ?""",
            (new_local_path, now, unique_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DownloadRecord:
        purged_at = datetime.fromisoformat(row["purged_at"]) if row["purged_at"] else None
        is_purged = bool(row["is_purged"])
        return DownloadRecord(
            unique_id=row["unique_id"],
            source_type=row["source_type"],
            source_identifier=row["source_identifier"],
            key=row["key"],
            local_path=row["local_path"],
            downloaded_at=datetime.fromisoformat(row["downloaded_at"]),
            is_purged=is_purged,
            purged_at=purged_at,
        )
