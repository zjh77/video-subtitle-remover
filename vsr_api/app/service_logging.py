"""Redacted, persistent service logging for the loopback VSR API."""

from __future__ import annotations

import logging
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from vsr_worker.redact import redact_text

from .config import LOG_BACKUP_COUNT, LOG_MAX_BYTES, LOGS_ROOT


LOGGER_NAME = "vsr_api.service"


def configure_api_logging(directory: Path | None = None) -> logging.Logger:
    """Configure one rotating file handler, even if startup runs more than once."""
    log_dir = directory or LOGS_ROOT
    log_dir.mkdir(parents=True, exist_ok=True)
    path = (log_dir / "vsr-api.log").resolve()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(Path(getattr(handler, "baseFilename", "")).resolve() == path for handler in logger.handlers):
        handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def log_event(level: int, event: str, *, job_id: str | None = None, **fields: Any) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        logger = configure_api_logging()
    values = [f"event={redact_text(event)}"]
    if job_id:
        values.append(f"job_id={redact_text(job_id)}")
    values.extend(f"{key}={redact_text(value)}" for key, value in sorted(fields.items()))
    logger.log(level, " ".join(values))


def log_exception(event: str, *, job_id: str | None = None, **fields: Any) -> None:
    log_event(logging.ERROR, event, job_id=job_id, **fields)
    logger = logging.getLogger(LOGGER_NAME)
    logger.error("event=%s job_id=%s traceback=%s", redact_text(event), redact_text(job_id or ""), redact_text(traceback.format_exc()))
