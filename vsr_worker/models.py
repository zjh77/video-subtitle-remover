"""Strict models for the video-task-server Worker lease contract."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

class ContractError(ValueError): pass

@dataclass(frozen=True)
class InputArtifact:
    artifact_id: str
    size_bytes: int
    sha256: str
    download_url: str

@dataclass(frozen=True)
class SubtitleCleanupOptions:
    subtitle_areas: list[dict[str, int]]
    inpaint_mode: str
    @classmethod
    def from_operation(cls, operation: Any) -> "SubtitleCleanupOptions":
        if not isinstance(operation, dict) or operation.get("type") != "subtitle_cleanup": raise ContractError("lease operation must be subtitle_cleanup")
        options = operation.get("options")
        if not isinstance(options, dict): raise ContractError("lease operation options are required")
        mode, areas = options.get("inpaint_mode"), options.get("subtitle_areas")
        if not isinstance(mode, str) or not mode or len(mode) > 128 or not isinstance(areas, list) or not areas: raise ContractError("lease cleanup options are invalid")
        parsed = []
        for area in areas:
            if not isinstance(area, dict) or set(area) != {"ymin", "ymax", "xmin", "xmax"} or any(type(area[key]) is not int for key in area): raise ContractError("lease subtitle area is invalid")
            if area["ymin"] < 0 or area["xmin"] < 0 or area["ymin"] >= area["ymax"] or area["xmin"] >= area["xmax"]: raise ContractError("lease subtitle bounds are invalid")
            parsed.append({key: area[key] for key in ("ymin", "ymax", "xmin", "xmax")})
        return cls(parsed, mode)

@dataclass(frozen=True)
class ClaimedLease:
    lease_id: str
    job_id: str
    attempt: int
    lease_expires_at: str
    input_artifact: InputArtifact
    cleanup: SubtitleCleanupOptions
    cancel_requested: bool = False
    trace_id: str | None = None
    client_request_id: str | None = None
    @classmethod
    def from_response(cls, value: dict[str, Any]) -> "ClaimedLease":
        try:
            source = value["input"]
            job = value.get("job", {})
            if not isinstance(job, dict): job = {}
            result = cls(str(value["lease_id"]), str(value["job_id"]), int(value["attempt"]), str(value["lease_expires_at"]), InputArtifact(str(source["artifact_id"]), int(source["size_bytes"]), str(source["sha256"]), str(source["download_url"])), SubtitleCleanupOptions.from_operation(value["operation"]), bool(value.get("cancel_requested", False)), _optional_id(value.get("trace_id", job.get("trace_id"))), _optional_id(value.get("client_request_id", job.get("client_request_id"))))
        except (KeyError, TypeError, ValueError) as exc: raise ContractError("lease claim response is invalid") from exc
        if not result.lease_id or not result.job_id or result.attempt < 1 or result.input_artifact.size_bytes <= 0: raise ContractError("lease claim response is invalid")
        return result

def _optional_id(value: Any) -> str | None:
    if value is None: return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256: raise ContractError("lease correlation ID is invalid")
    return value.strip()

@dataclass(frozen=True)
class UploadSession:
    upload_id: str
    artifact_id: str
    part_size_bytes: int
    reused: bool
