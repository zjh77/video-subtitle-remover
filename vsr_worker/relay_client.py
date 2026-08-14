"""Authenticated HTTPS adapter for the public relay task center.

Endpoint names intentionally live in one adapter.  Once the task-center
OpenAPI document is final, only this module should require contract changes.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .config import RelaySettings
from .models import ClaimedTask, UploadSession


class RelayError(RuntimeError):
    pass


class RelayProtocolError(RelayError):
    pass


class LeaseLostError(RelayError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class HeartbeatResult:
    cancel_requested: bool


class RelayClient:
    """Relay calls with private-CA TLS verification and redaction-safe errors."""

    API_PREFIX = "/api/v1"

    def __init__(self, settings: RelaySettings):
        self.settings = settings
        self._origin = settings.base_url.rstrip("/")
        self._origin_parts = urlsplit(self._origin)
        self._ssl_context = ssl.create_default_context(cafile=str(settings.ca_file))
        self._ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=self._ssl_context))

    def register(self, capabilities: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/workers/register", {"worker_id": self.settings.worker_id, "capabilities": capabilities})

    def heartbeat(self, current: dict[str, Any] | None = None) -> HeartbeatResult:
        value = self._json("POST", f"/workers/{self._worker_path()}/heartbeat", {"current": current})
        return HeartbeatResult(bool(value.get("cancel_requested", False)))

    def claim(self, wait_seconds: int, capabilities: dict[str, Any]) -> ClaimedTask | None:
        status, value = self._json_with_status(
            "POST",
            f"/workers/{self._worker_path()}/claim",
            {"wait_seconds": wait_seconds, "capabilities": capabilities},
            timeout=wait_seconds + 15,
            allow_no_content=True,
        )
        return None if status == 204 else ClaimedTask.from_response(value)

    def get_attempt(self, task_id: str, attempt_id: str) -> ClaimedTask:
        value = self._json("GET", f"/tasks/{quote(task_id, safe='')}/attempts/{quote(attempt_id, safe='')}")
        return ClaimedTask.from_response(value)

    def renew_lease(self, claim: ClaimedTask) -> HeartbeatResult:
        value = self._json("POST", self._attempt_path(claim) + "/lease/renew", {"lease_token": claim.lease_token})
        if value.get("lease_valid") is False:
            raise LeaseLostError("relay rejected the current task lease")
        return HeartbeatResult(bool(value.get("cancel_requested", False)))

    def report_progress(self, claim: ClaimedTask, stage: str, progress: float | None, message: str) -> None:
        payload: dict[str, Any] = {"lease_token": claim.lease_token, "stage": stage, "message": message}
        if progress is not None:
            payload["progress"] = max(0.0, min(100.0, float(progress)))
        self._json("POST", self._attempt_path(claim) + "/progress", payload)

    def report_logs(self, claim: ClaimedTask, items: list[dict[str, Any]]) -> None:
        if items:
            self._json("POST", self._attempt_path(claim) + "/logs", {"lease_token": claim.lease_token, "items": items})

    def create_output_upload(self, claim: ClaimedTask, size_bytes: int, sha256: str, metadata: dict[str, Any]) -> UploadSession:
        value = self._json(
            "POST",
            self._attempt_path(claim) + "/output-upload",
            {"lease_token": claim.lease_token, "size_bytes": size_bytes, "sha256": sha256, "video": metadata},
        )
        return UploadSession.from_response(value)

    def upload_part(self, claim: ClaimedTask, upload_id: str, part_number: int, content_range: str, part_sha256: str, content: bytes) -> str:
        value = self._json(
            "PUT",
            f"/uploads/{quote(upload_id, safe='')}/parts/{part_number}",
            content,
            headers={"X-Lease-Token": claim.lease_token, "Content-Range": content_range, "X-Part-SHA256": part_sha256, "Content-Type": "application/octet-stream"},
        )
        try:
            return str(value["etag"])
        except KeyError as exc:
            raise RelayProtocolError("relay upload part response omitted its ETag") from exc

    def complete_output_upload(self, claim: ClaimedTask, upload_id: str, parts: list[dict[str, Any]], size_bytes: int, sha256: str) -> str:
        value = self._json(
            "POST",
            f"/uploads/{quote(upload_id, safe='')}/complete",
            {"lease_token": claim.lease_token, "parts": parts, "size_bytes": size_bytes, "sha256": sha256},
        )
        try:
            return str(value["output_asset_id"])
        except KeyError as exc:
            raise RelayProtocolError("relay output completion response omitted asset ID") from exc

    def complete_task(self, claim: ClaimedTask, output_asset_id: str) -> None:
        self._json("POST", self._attempt_path(claim) + "/complete", {"lease_token": claim.lease_token, "output_asset_id": output_asset_id})

    def fail_task(self, claim: ClaimedTask, error_code: str, message: str) -> None:
        self._json("POST", self._attempt_path(claim) + "/fail", {"lease_token": claim.lease_token, "error_code": error_code, "message": message})

    def cancel_task(self, claim: ClaimedTask, message: str = "Cancelled by Workbench request.") -> None:
        self._json("POST", self._attempt_path(claim) + "/cancelled", {"lease_token": claim.lease_token, "message": message})

    def open_input(self, url: str, start_at: int):
        """Open an HTTPS Range request without forwarding the Worker Token."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != self._origin_parts.netloc:
            raise RelayProtocolError("relay supplied an input URL outside its HTTPS origin")
        headers = {"Range": f"bytes={start_at}-"} if start_at else {}
        request = Request(url, headers=headers, method="GET")
        try:
            return self._opener.open(request, timeout=60)
        except HTTPError as exc:
            raise RelayError(f"relay input download returned HTTP {exc.code}") from exc
        except (URLError, ssl.SSLError) as exc:
            raise RelayError("relay input download connection failed") from exc

    def _attempt_path(self, claim: ClaimedTask) -> str:
        return f"/tasks/{quote(claim.task_id, safe='')}/attempts/{quote(claim.attempt_id, safe='')}"

    def _worker_path(self) -> str:
        return quote(self.settings.worker_id, safe="")

    def _json(self, method: str, path: str, payload: Any = None, headers: dict[str, str] | None = None, timeout: int = 30) -> dict[str, Any]:
        _, value = self._json_with_status(method, path, payload, headers=headers, timeout=timeout)
        return value

    def _json_with_status(self, method: str, path: str, payload: Any = None, headers: dict[str, str] | None = None, timeout: int = 30, allow_no_content: bool = False) -> tuple[int, dict[str, Any]]:
        url = urljoin(self._origin + self.API_PREFIX + "/", path.lstrip("/"))
        request_headers = {"Accept": "application/json", "Authorization": f"Bearer {self.settings.worker_token}", "User-Agent": "vsr-worker/0.1"}
        if headers:
            request_headers.update(headers)
        body: bytes | None
        if payload is None:
            body = None
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                raw = response.read()
        except HTTPError as exc:
            if allow_no_content and exc.code == 204:
                return 204, {}
            if exc.code in {401, 403, 409, 410, 412}:
                raise LeaseLostError(f"relay rejected request with HTTP {exc.code}") from exc
            raise RelayError(f"relay request returned HTTP {exc.code}") from exc
        except (URLError, ssl.SSLError) as exc:
            raise RelayError("relay HTTPS connection failed") from exc
        if status == 204 and allow_no_content:
            return status, {}
        if not raw:
            raise RelayProtocolError("relay response unexpectedly had no JSON body")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RelayProtocolError("relay response was not valid JSON") from exc
        if not isinstance(value, dict):
            raise RelayProtocolError("relay response JSON must be an object")
        return status, value
