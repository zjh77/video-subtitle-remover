"""Utilities that keep credentials and signed URLs out of diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_SENSITIVE_KEY = re.compile(
    r"(authorization|token|secret|password|private|certificate|signature|credential|api[_-]?key)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)bearer\s+[^\s,;]+")
_URL = re.compile(r"https?://[^\s\"']+")


def redact_text(value: object) -> str:
    """Return a string suitable for logs without credentials or URL queries."""
    text = str(value)
    text = _BEARER.sub("Bearer [REDACTED]", text)

    def replace_url(match: re.Match[str]) -> str:
        parts = urlsplit(match.group(0))
        # Query parameters are where signed object URLs normally carry secrets.
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "[REDACTED]" if parts.query else "", ""))

    return _URL.sub(replace_url, text)


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a mapping while replacing values whose key is sensitive."""
    result: dict[str, Any] = {}
    for key, item in value.items():
        if _SENSITIVE_KEY.search(key):
            result[key] = "[REDACTED]"
        elif isinstance(item, Mapping):
            result[key] = redact_mapping(item)
        elif isinstance(item, list):
            result[key] = [redact_mapping(part) if isinstance(part, Mapping) else redact_text(part) for part in item]
        else:
            result[key] = redact_text(item)
    return result
