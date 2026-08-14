"""Data structures shared by relay, transfer, state, and orchestration code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InputAsset:
    asset_id: str
    filename: str
    size_bytes: int
    sha256: str
    download_url: str


@dataclass(frozen=True)
class CleanupRequest:
    subtitle_areas: list[dict[str, int]]
    inpaint_mode: str


@dataclass(frozen=True)
class ClaimedTask:
    task_id: str
    attempt_id: str
    lease_token: str
    lease_expires_at: str
    input_asset: InputAsset
    cleanup: CleanupRequest

    @classmethod
    def from_response(cls, value: dict[str, Any]) -> "ClaimedTask":
        """Parse the documented relay claim shape without echoing sensitive URLs."""
        try:
            source = value["input"]
            cleanup = value["cleanup"]
            asset = InputAsset(
                asset_id=str(source["asset_id"]),
                filename=str(source["filename"]),
                size_bytes=int(source["size_bytes"]),
                sha256=str(source["sha256"]),
                download_url=str(source["download_url"]),
            )
            return cls(
                task_id=str(value["task_id"]),
                attempt_id=str(value["attempt_id"]),
                lease_token=str(value["lease_token"]),
                lease_expires_at=str(value["lease_expires_at"]),
                input_asset=asset,
                cleanup=CleanupRequest(list(cleanup["subtitle_areas"]), str(cleanup["inpaint_mode"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("relay claim response is missing required task fields") from exc


@dataclass(frozen=True)
class UploadSession:
    upload_id: str
    part_size_bytes: int
    uploaded_parts: dict[int, str]

    @classmethod
    def from_response(cls, value: dict[str, Any]) -> "UploadSession":
        try:
            parts = {int(item["part_number"]): str(item["etag"]) for item in value.get("uploaded_parts", [])}
            return cls(str(value["upload_id"]), int(value["part_size_bytes"]), parts)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("relay upload session response is invalid") from exc
