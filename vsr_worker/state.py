"""Crash-safe local state keyed by formal job_id and lease_id."""
from __future__ import annotations
import json, sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

@dataclass(frozen=True)
class LeaseRecord:
    job_id: str; lease_id: str; attempt: int; lease_expires_at: str; status: str; input_artifact_id: str; input_size_bytes: int; input_sha256: str; operation_json: str; trace_id: str | None; client_request_id: str | None; local_input_asset_id: str | None; local_vsr_job_id: str | None; local_output_asset_id: str | None; log_sequence: int; upload_id: str | None; upload_part_size: int | None; updated_at: str

class WorkerState:
    def __init__(self, state_dir: Path): self.root, self.path = state_dir, state_dir / "worker-state.db"
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True); connection = sqlite3.connect(self.path); connection.row_factory = sqlite3.Row
        try: yield connection; connection.commit()
        finally: connection.close()
    def initialize(self) -> None:
        with self._connection() as c:
            c.executescript("""PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS leases (job_id TEXT NOT NULL, lease_id TEXT PRIMARY KEY, attempt INTEGER NOT NULL, lease_expires_at TEXT NOT NULL, status TEXT NOT NULL, input_artifact_id TEXT NOT NULL, input_size_bytes INTEGER NOT NULL, input_sha256 TEXT NOT NULL, operation_json TEXT NOT NULL, trace_id TEXT, client_request_id TEXT, local_input_asset_id TEXT, local_vsr_job_id TEXT, local_output_asset_id TEXT, log_sequence INTEGER NOT NULL DEFAULT 0, upload_id TEXT, upload_part_size INTEGER, updated_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS upload_parts (lease_id TEXT NOT NULL, part_number INTEGER NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY (lease_id, part_number));""")
            existing = {row[1] for row in c.execute("PRAGMA table_info(leases)")}
            for column in ("trace_id", "client_request_id"):
                if column not in existing: c.execute(f"ALTER TABLE leases ADD COLUMN {column} TEXT")
    def save_claim(self, claim: Any) -> None:
        operation = {"type":"subtitle_cleanup","options":{"subtitle_areas":claim.cleanup.subtitle_areas,"inpaint_mode":claim.cleanup.inpaint_mode}}
        with self._connection() as c: c.execute("""INSERT INTO leases(job_id,lease_id,attempt,lease_expires_at,status,input_artifact_id,input_size_bytes,input_sha256,operation_json,trace_id,client_request_id,log_sequence,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(lease_id) DO UPDATE SET job_id=excluded.job_id,attempt=excluded.attempt,lease_expires_at=excluded.lease_expires_at,input_artifact_id=excluded.input_artifact_id,input_size_bytes=excluded.input_size_bytes,input_sha256=excluded.input_sha256,operation_json=excluded.operation_json,trace_id=excluded.trace_id,client_request_id=excluded.client_request_id,updated_at=excluded.updated_at""", (claim.job_id,claim.lease_id,claim.attempt,claim.lease_expires_at,"claimed",claim.input_artifact.artifact_id,claim.input_artifact.size_bytes,claim.input_artifact.sha256,json.dumps(operation,separators=(",",":")),claim.trace_id,claim.client_request_id,0,self._now()))
    def get(self, lease_id: str) -> LeaseRecord | None:
        with self._connection() as c: row=c.execute("SELECT * FROM leases WHERE lease_id=?",(lease_id,)).fetchone()
        return LeaseRecord(**dict(row)) if row else None
    def unfinished(self) -> list[LeaseRecord]:
        with self._connection() as c: rows=c.execute("SELECT * FROM leases WHERE status NOT IN ('succeeded','failed','cancelled','lease_lost') ORDER BY job_id").fetchall()
        return [LeaseRecord(**dict(row)) for row in rows]
    def update(self, lease_id: str, **values: Any) -> None:
        allowed={"lease_expires_at","status","local_input_asset_id","local_vsr_job_id","local_output_asset_id","log_sequence","upload_id","upload_part_size"}
        if set(values)-allowed: raise ValueError("invalid lease state update")
        if values:
            with self._connection() as c: c.execute(f"UPDATE leases SET {','.join(f'{key}=?' for key in values)},updated_at=? WHERE lease_id=?",[*values.values(),self._now(),lease_id])
    def save_upload_part(self, lease_id: str, part_number: int, sha256: str) -> None:
        with self._connection() as c: c.execute("INSERT OR REPLACE INTO upload_parts VALUES (?,?,?)",(lease_id,part_number,sha256))
    def upload_parts(self, lease_id: str) -> list[dict[str,Any]]:
        with self._connection() as c: rows=c.execute("SELECT part_number,sha256 FROM upload_parts WHERE lease_id=? ORDER BY part_number",(lease_id,)).fetchall()
        return [dict(row) for row in rows]
    def remove(self, lease_id: str) -> None:
        with self._connection() as c: c.execute("DELETE FROM upload_parts WHERE lease_id=?",(lease_id,)); c.execute("DELETE FROM leases WHERE lease_id=?",(lease_id,))
    def terminal_before(self, cutoff: str) -> list[LeaseRecord]:
        with self._connection() as c: rows=c.execute("SELECT * FROM leases WHERE status IN ('succeeded','failed','cancelled','lease_lost') AND updated_at < ?",(cutoff,)).fetchall()
        return [LeaseRecord(**dict(row)) for row in rows]
    @staticmethod
    def _now() -> str: return datetime.now(timezone.utc).isoformat()
