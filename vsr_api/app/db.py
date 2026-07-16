from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .config import DB_PATH, ensure_data_dirs


@contextmanager
def conn():
    ensure_data_dirs(); connection = sqlite3.connect(DB_PATH); connection.row_factory = sqlite3.Row
    try: yield connection; connection.commit()
    finally: connection.close()


def init_db() -> None:
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, filename TEXT NOT NULL, path TEXT NOT NULL, size_bytes INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, fps REAL NOT NULL, duration_seconds REAL NOT NULL, created_at TEXT NOT NULL, source_job_id TEXT);
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, input_asset_id TEXT NOT NULL, output_asset_id TEXT, status TEXT NOT NULL, inpaint_mode TEXT NOT NULL, subtitle_areas TEXT NOT NULL, progress REAL, stage TEXT, message TEXT, error_code TEXT, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT);
        CREATE TABLE IF NOT EXISTS logs (job_id TEXT NOT NULL, sequence INTEGER NOT NULL, timestamp TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, PRIMARY KEY(job_id, sequence));
        """)


def insert(table: str, values: dict[str, Any]) -> None:
    with conn() as c: c.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))


def update_job(job_id: str, **values: Any) -> None:
    if values:
        with conn() as c: c.execute(f"UPDATE jobs SET {','.join(f'{key}=?' for key in values)} WHERE id=?", [*values.values(), job_id])


def get_asset(asset_id: str) -> dict | None:
    with conn() as c: row = c.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    return dict(row) if row else None


def get_job(job_id: str) -> dict | None:
    with conn() as c: row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def recover_jobs() -> list[str]:
    """A worker process cannot survive a service restart; safely requeue only untouched jobs."""
    with conn() as c:
        c.execute("UPDATE jobs SET status='failed', stage='failed', error_code='SERVICE_RESTARTED', message='Service restarted while the task was executing.', completed_at=? WHERE status IN ('running','cancelling')", (datetime.now(timezone.utc).isoformat(),))
        rows = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at").fetchall()
    return [row["id"] for row in rows]


def jobs_using_asset(asset_id: str) -> list[dict]:
    with conn() as c: rows = c.execute("SELECT * FROM jobs WHERE input_asset_id=? AND status IN ('queued','running','cancelling')", (asset_id,)).fetchall()
    return [dict(row) for row in rows]


def delete_asset(asset_id: str) -> None:
    with conn() as c: c.execute("DELETE FROM assets WHERE id=?", (asset_id,))


def append_log(job_id: str, timestamp: str, level: str, message: str) -> None:
    with conn() as c:
        sequence = c.execute("SELECT COALESCE(MAX(sequence), 0)+1 FROM logs WHERE job_id=?", (job_id,)).fetchone()[0]
        c.execute("INSERT INTO logs VALUES (?,?,?,?,?)", (job_id, sequence, timestamp, level, message))


def get_logs(job_id: str, after: int) -> list[dict]:
    with conn() as c: rows = c.execute("SELECT sequence,timestamp,level,message FROM logs WHERE job_id=? AND sequence>? ORDER BY sequence", (job_id, after)).fetchall()
    return [dict(row) for row in rows]
