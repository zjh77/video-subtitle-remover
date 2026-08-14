"""Crash-safe local state for one-at-a-time relay attempts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class AttemptRecord:
    task_id: str
    attempt_id: str
    lease_token: str
    lease_expires_at: str
    status: str
    input_asset_id: str
    input_filename: str
    input_size_bytes: int
    input_sha256: str
    cleanup_json: str
    local_input_asset_id: str | None
    local_vsr_job_id: str | None
    local_output_asset_id: str | None
    log_sequence: int
    upload_id: str | None
    upload_part_size: int | None
    updated_at: str


class WorkerState:
    def __init__(self, state_dir: Path):
        self.root = state_dir
        self.path = state_dir / "worker-state.db"

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS attempts (
                    task_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    lease_token TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_asset_id TEXT NOT NULL,
                    input_filename TEXT NOT NULL,
                    input_size_bytes INTEGER NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    cleanup_json TEXT NOT NULL,
                    local_input_asset_id TEXT,
                    local_vsr_job_id TEXT,
                    local_output_asset_id TEXT,
                    log_sequence INTEGER NOT NULL DEFAULT 0,
                    upload_id TEXT,
                    upload_part_size INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, attempt_id)
                );
                CREATE TABLE IF NOT EXISTS upload_parts (
                    task_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    part_number INTEGER NOT NULL,
                    etag TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY (task_id, attempt_id, part_number)
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
            if "local_input_asset_id" not in columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN local_input_asset_id TEXT")
            if "local_output_asset_id" not in columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN local_output_asset_id TEXT")
            if "updated_at" not in columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN updated_at TEXT")
                connection.execute("UPDATE attempts SET updated_at=? WHERE updated_at IS NULL", (self._now(),))

    def save_claim(self, claim: Any) -> None:
        """Persist only durable metadata; signed URLs are intentionally excluded."""
        cleanup = {"subtitle_areas": claim.cleanup.subtitle_areas, "inpaint_mode": claim.cleanup.inpaint_mode}
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO attempts (
                    task_id,attempt_id,lease_token,lease_expires_at,status,input_asset_id,input_filename,
                    input_size_bytes,input_sha256,cleanup_json,local_input_asset_id,local_vsr_job_id,
                    local_output_asset_id,log_sequence,upload_id,upload_part_size,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id,attempt_id) DO UPDATE SET
                  lease_token=excluded.lease_token, lease_expires_at=excluded.lease_expires_at,
                  status=excluded.status, input_asset_id=excluded.input_asset_id,
                  input_filename=excluded.input_filename, input_size_bytes=excluded.input_size_bytes,
                  input_sha256=excluded.input_sha256, cleanup_json=excluded.cleanup_json,
                  updated_at=excluded.updated_at""",
                (
                    claim.task_id, claim.attempt_id, claim.lease_token, claim.lease_expires_at, "claimed",
                    claim.input_asset.asset_id, claim.input_asset.filename, claim.input_asset.size_bytes,
                    claim.input_asset.sha256, json.dumps(cleanup, separators=(",", ":")), None, None, None, 0, None, None, self._now(),
                ),
            )

    def get(self, task_id: str, attempt_id: str) -> AttemptRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE task_id=? AND attempt_id=?", (task_id, attempt_id)).fetchone()
        return AttemptRecord(**dict(row)) if row else None

    def unfinished(self) -> list[AttemptRecord]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM attempts WHERE status NOT IN ('succeeded','failed','cancelled') ORDER BY task_id").fetchall()
        return [AttemptRecord(**dict(row)) for row in rows]

    def update(self, task_id: str, attempt_id: str, **values: Any) -> None:
        allowed = {"lease_token", "lease_expires_at", "status", "local_input_asset_id", "local_vsr_job_id", "local_output_asset_id", "log_sequence", "upload_id", "upload_part_size"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("invalid local attempt state update")
        if not values:
            return
        with self._connection() as connection:
            connection.execute(
                f"UPDATE attempts SET {','.join(f'{key}=?' for key in values)},updated_at=? WHERE task_id=? AND attempt_id=?",
                [*values.values(), self._now(), task_id, attempt_id],
            )

    def save_upload_part(self, task_id: str, attempt_id: str, part_number: int, etag: str, sha256: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO upload_parts VALUES (?,?,?,?,?)",
                (task_id, attempt_id, part_number, etag, sha256),
            )

    def upload_parts(self, task_id: str, attempt_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT part_number,etag,sha256 FROM upload_parts WHERE task_id=? AND attempt_id=? ORDER BY part_number", (task_id, attempt_id)).fetchall()
        return [dict(row) for row in rows]

    def remove(self, task_id: str, attempt_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM upload_parts WHERE task_id=? AND attempt_id=?", (task_id, attempt_id))
            connection.execute("DELETE FROM attempts WHERE task_id=? AND attempt_id=?", (task_id, attempt_id))

    def terminal_before(self, cutoff: str) -> list[AttemptRecord]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM attempts WHERE status IN ('succeeded','failed','cancelled','lease_lost') AND updated_at < ?", (cutoff,)).fetchall()
        return [AttemptRecord(**dict(row)) for row in rows]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
