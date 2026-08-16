"""Strict, secret-safe configuration for the outbound relay Worker."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised before networking starts when local configuration is invalid."""


def _positive_int(value: Any, name: str, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if number < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return number


def _nested(data: dict[str, Any], section: str, key: str, default: Any = "") -> Any:
    value = data.get(section, {})
    return value.get(key, default) if isinstance(value, dict) else default


def _read_local_config() -> dict[str, Any]:
    filename = os.getenv("VSR_RELAY_CONFIG_FILE", "").strip()
    if not filename:
        return {}
    path = Path(filename).expanduser()
    try:
        with path.open(encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("could not read protected relay configuration") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("protected relay configuration must be a JSON object")
    return parsed


def _setting(data: dict[str, Any], env_name: str, section: str, key: str, default: Any = "") -> Any:
    # Environment values override the ignored local file, which overrides examples.
    return os.getenv(env_name, _nested(data, section, key, default))


def _read_token(data: dict[str, Any]) -> str:
    direct = os.getenv("VSR_RELAY_WORKER_TOKEN", _nested(data, "relay", "worker_token", "")).strip()
    token_file = os.getenv("VSR_RELAY_WORKER_TOKEN_FILE", _nested(data, "relay", "token_file", "")).strip()
    if direct and token_file:
        raise ConfigError("configure either a Worker token or token file, not both")
    if token_file:
        try:
            direct = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("could not read protected Worker token file") from exc
    if len(direct) < 32:
        raise ConfigError("Worker token is missing or too short")
    return direct


@dataclass(frozen=True)
class RelaySettings:
    base_url: str
    ca_file: Path
    worker_id: str
    worker_token: str = field(repr=False)


@dataclass(frozen=True)
class RuntimeSettings:
    state_dir: Path
    min_free_bytes: int
    heartbeat_seconds: int
    claim_timeout_seconds: int
    lease_seconds: int
    lease_renew_seconds: int
    local_vsr_base_url: str
    log_dir: Path | None
    log_max_bytes: int
    log_backup_count: int


@dataclass(frozen=True)
class WorkerSettings:
    relay: RelaySettings
    runtime: RuntimeSettings


def load_settings() -> WorkerSettings:
    """Load runtime-only settings without ever logging sensitive source values."""
    data = _read_local_config()
    base_url = str(_setting(data, "VSR_RELAY_BASE_URL", "relay", "base_url")).strip().rstrip("/")
    parsed_url = urlsplit(base_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.query or parsed_url.fragment:
        raise ConfigError("VSR_RELAY_BASE_URL must be an HTTPS origin without a query")

    ca_text = str(_setting(data, "VSR_RELAY_CA_FILE", "relay", "ca_file")).strip()
    if not ca_text:
        raise ConfigError("VSR_RELAY_CA_FILE is required")
    ca_file = Path(ca_text).expanduser()
    if not ca_file.is_file():
        raise ConfigError("configured relay CA file does not exist")

    worker_id = str(_setting(data, "VSR_RELAY_WORKER_ID", "relay", "worker_id")).strip()
    if not worker_id:
        raise ConfigError("VSR_RELAY_WORKER_ID is required")

    state_text = str(_setting(data, "VSR_RELAY_STATE_DIR", "runtime", "state_dir")).strip()
    if not state_text:
        raise ConfigError("VSR_RELAY_STATE_DIR is required")
    state_dir = Path(state_text).expanduser()

    local_vsr = str(_setting(data, "VSR_RELAY_LOCAL_VSR_URL", "local_vsr", "base_url", "http://127.0.0.1:8020")).strip().rstrip("/")
    local_url = urlsplit(local_vsr)
    if local_url.scheme != "http" or local_url.hostname not in {"127.0.0.1", "::1", "localhost"} or local_url.query:
        raise ConfigError("VSR_RELAY_LOCAL_VSR_URL must be an HTTP loopback origin")

    heartbeat = _positive_int(_setting(data, "VSR_RELAY_HEARTBEAT_SECONDS", "runtime", "heartbeat_seconds", 20), "heartbeat_seconds", 15)
    if heartbeat > 20:
        raise ConfigError("heartbeat_seconds must not exceed 20")
    lease = _positive_int(_setting(data, "VSR_RELAY_LEASE_SECONDS", "runtime", "lease_seconds", 90), "lease_seconds", 45)
    renew = _positive_int(_setting(data, "VSR_RELAY_LEASE_RENEW_SECONDS", "runtime", "lease_renew_seconds", 20), "lease_renew_seconds", 10)
    if renew >= lease:
        raise ConfigError("lease_renew_seconds must be lower than lease_seconds")

    runtime = RuntimeSettings(
        state_dir=state_dir,
        min_free_bytes=_positive_int(_setting(data, "VSR_RELAY_MIN_FREE_BYTES", "runtime", "min_free_bytes", 2147483648), "min_free_bytes", 0),
        heartbeat_seconds=heartbeat,
        claim_timeout_seconds=_positive_int(_setting(data, "VSR_RELAY_CLAIM_TIMEOUT_SECONDS", "runtime", "claim_timeout_seconds", 20), "claim_timeout_seconds", 1),
        lease_seconds=lease,
        lease_renew_seconds=renew,
        local_vsr_base_url=local_vsr,
        log_dir=Path(str(_setting(data, "VSR_RELAY_LOG_DIR", "observability", "log_dir")).strip()).expanduser() if str(_setting(data, "VSR_RELAY_LOG_DIR", "observability", "log_dir")).strip() else None,
        log_max_bytes=_positive_int(_setting(data, "VSR_RELAY_LOG_MAX_BYTES", "observability", "log_max_bytes", 10485760), "log_max_bytes"),
        log_backup_count=_positive_int(_setting(data, "VSR_RELAY_LOG_BACKUP_COUNT", "observability", "log_backup_count", 7), "log_backup_count", 1),
    )
    if runtime.claim_timeout_seconds > 25:
        raise ConfigError("claim_timeout_seconds must not exceed 25")
    return WorkerSettings(RelaySettings(base_url, ca_file, worker_id, _read_token(data)), runtime)
