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
        self.logger = logging.getLogger(f"vsr_worker.audit.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = RotatingFileHandler(self.path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)

    def emit(self, level: str, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": str(level).upper(),
            "event": redact_text(event),
            **redact_mapping(fields),
        }
        try:
            self.logger.log(getattr(logging, payload["level"], logging.INFO), json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            # Observability must never cause a video task to fail.
            pass
        finally:
            # The single Worker is sequential. Closing after each record avoids
            # holding a Windows file handle that would prevent retention cleanup.
            for handler in self.logger.handlers:
                try:
                    handler.flush()
                    handler.close()
                except Exception:
                    pass
