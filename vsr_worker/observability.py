"""Safe, structured, local audit logging for the outbound Worker."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .redact import redact_mapping, redact_text


class JsonlAuditLogger:
    """Writes one redacted JSON object per line without leaking runtime settings."""

    def __init__(self, directory: Path, max_bytes: int, backup_count: int):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "worker.jsonl"
        self.summary_path = directory / "worker-summary.log"
        self.logger = self._logger("audit", self.path, max_bytes, backup_count)
        self.summary_logger = self._logger("summary", self.summary_path, max_bytes, backup_count)

    @staticmethod
    def _logger(kind: str, path: Path, max_bytes: int, backup_count: int) -> logging.Logger:
        logger = logging.getLogger(f"vsr_worker.{kind}.{id(path)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger

    def emit(self, level: str, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": str(level).upper(),
            "event": redact_text(event),
            **redact_mapping(fields),
        }
        try:
            self.logger.log(getattr(logging, payload["level"], logging.INFO), json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            if event in _SUMMARY_EVENTS:
                self.summary_logger.log(getattr(logging, payload["level"], logging.INFO), _summary_line(payload))
        except Exception:
            # Observability must never cause a video task to fail.
            pass
        finally:
            # The single Worker is sequential. Closing after each record avoids
            # holding a Windows file handle that would prevent retention cleanup.
            for logger in (self.logger, self.summary_logger):
             for handler in logger.handlers:
                try:
                    handler.flush()
                    handler.close()
                except Exception:
                    pass

    def close(self) -> None:
        for logger in (self.logger, self.summary_logger):
            for handler in list(logger.handlers):
                try:
                    handler.close()
                finally:
                    logger.removeHandler(handler)


_SUMMARY_EVENTS = frozenset({
    "startup_validated", "local_vsr_healthy", "worker_registered", "reconnect_wait", "reconnected",
    "lease_claimed", "input_download_started", "input_download_completed", "input_download_failed",
    "local_vsr_started", "local_vsr_completed", "output_upload_started", "output_upload_completed",
    "output_upload_failed", "task_completed", "task_failed", "task_cancelled", "lease_lost",
})
_SUMMARY_FIELDS = ("trace_id", "job_id", "lease_id", "attempt", "local_vsr_job_id", "size_bytes", "transferred_bytes", "resumed", "elapsed_ms", "throughput_mbps", "error_code", "error")


def _summary_line(payload: dict[str, Any]) -> str:
    values = [payload["timestamp"], payload["level"], payload["event"]]
    for key in _SUMMARY_FIELDS:
        value = payload.get(key)
        if value is not None and value != "None":
            values.append(f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")
    return " ".join(values)
