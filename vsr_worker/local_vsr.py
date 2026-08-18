"""Loopback-only adapter for the existing vsr_api service."""

from __future__ import annotations

import http.client
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit


class LocalVsrError(RuntimeError):
    pass


class LocalVsrTransientError(LocalVsrError):
    """A loopback HTTP transport interruption that may safely be retried."""


@dataclass
class DownloadResponse:
    connection: http.client.HTTPConnection
    response: http.client.HTTPResponse

    @property
    def status(self) -> int:
        return self.response.status

    def header(self, name: str) -> str | None:
        return self.response.getheader(name)

    def read(self, size: int = -1) -> bytes:
        return self.response.read(size)

    def close(self) -> None:
        self.response.close()
        self.connection.close()


class LocalVsrClient:
    def __init__(self, base_url: str, timeout_seconds: int = 60):
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("local VSR client requires an HTTP loopback URL")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.base_path = parsed.path.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/health")

    def upload_asset(self, path: Path, progress: Callable[[int, int], None] | None = None) -> str:
        boundary = f"----vsrworker{uuid.uuid4().hex}"
        filename = path.name.replace('"', "_")
        prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: video/mp4\r\n\r\n").encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        length = len(prefix) + path.stat().st_size + len(suffix)
        conn = self._connection()
        try:
            conn.putrequest("POST", self._path("/api/v1/assets"))
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(length))
            conn.endheaders()
            conn.send(prefix)
            sent = 0
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    conn.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, path.stat().st_size)
            conn.send(suffix)
            value = self._read_json_response(conn)
            return str(value["asset_id"])
        except (OSError, http.client.HTTPException, KeyError) as exc:
            raise LocalVsrTransientError("could not upload input video to local VSR") from exc

    def create_job(self, input_asset_id: str, subtitle_areas: list[dict[str, int]], inpaint_mode: str) -> str:
        value = self._json("POST", "/api/v1/jobs", {"input_asset_id": input_asset_id, "subtitle_areas": subtitle_areas, "inpaint_mode": inpaint_mode})
        try:
            return str(value["job_id"])
        except KeyError as exc:
            raise LocalVsrError("local VSR did not return a job ID") from exc

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/jobs/{quote(job_id, safe='')}")

    def get_logs(self, job_id: str, after: int) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/jobs/{quote(job_id, safe='')}/logs?after={after}")

    def cancel(self, job_id: str) -> str:
        value = self._json("POST", f"/api/v1/jobs/{quote(job_id, safe='')}/cancel", {})
        return str(value.get("status", "cancelling"))

    def open_output(self, asset_id: str, start_at: int) -> DownloadResponse:
        conn = self._connection()
        headers = {"Range": f"bytes={start_at}-"} if start_at else {}
        try:
            conn.request("GET", self._path(f"/api/v1/assets/{quote(asset_id, safe='')}/download"), headers=headers)
            response = conn.getresponse()
            if response.status not in {200, 206}:
                response.close(); conn.close()
                raise LocalVsrError(f"local VSR output download returned HTTP {response.status}")
            return DownloadResponse(conn, response)
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise LocalVsrTransientError("could not download local VSR output") from exc

    def download_output(self, asset_id: str, destination: Path) -> None:
        response = self.open_output(asset_id, 0)
        try:
            with destination.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
        finally:
            response.close()

    def delete_asset(self, asset_id: str) -> None:
        self._json("DELETE", f"/api/v1/assets/{quote(asset_id, safe='')}")

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        conn = self._connection()
        try:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, self._path(path), body=body, headers=headers)
            return self._read_json_response(conn)
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            # RemoteDisconnected and IncompleteRead are HTTPException values.
            # They are transient for a loopback polling request: the persisted
            # local VSR job ID lets the runtime ask again without starting work
            # a second time.
            raise LocalVsrTransientError("local VSR API request failed") from exc
        finally:
            conn.close()

    def _read_json_response(self, conn: http.client.HTTPConnection) -> dict[str, Any]:
        response = conn.getresponse()
        try:
            raw = response.read()
            if response.status < 200 or response.status >= 300:
                raise LocalVsrError(f"local VSR API returned HTTP {response.status}")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise LocalVsrError("local VSR API returned invalid JSON")
            return parsed
        finally:
            response.close()

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_seconds)

    def _path(self, suffix: str) -> str:
        return f"{self.base_path}{suffix}" or "/"
