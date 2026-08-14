"""Resumable, checksum-verified local transfers for the relay Worker."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Protocol

from .models import ClaimedTask
from .state import WorkerState


class BinaryResponse(Protocol):
    status: int

    def header(self, name: str) -> str | None: ...
    def read(self, size: int = -1) -> bytes: ...
    def close(self) -> None: ...


class TransferError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def available_bytes(path: Path) -> int:
    return os.statvfs(path).f_bavail * os.statvfs(path).f_frsize if hasattr(os, "statvfs") else __import__("shutil").disk_usage(path).free


def download_with_resume(destination: Path, expected_size: int, expected_sha256: str, open_response: Callable[[int], BinaryResponse], progress: Callable[[int, int], None] | None = None) -> Path:
    """Download to ``.part`` and atomically expose only a fully verified file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    current_size = partial.stat().st_size if partial.exists() else 0
    if current_size > expected_size:
        partial.unlink()
        current_size = 0
    if current_size == expected_size and sha256_file(partial).lower() == expected_sha256.lower():
        os.replace(partial, destination)
        return destination

    response = open_response(current_size)
    try:
        if current_size and response.status != 206:
            partial.unlink(missing_ok=True)
            current_size = 0
            response.close()
            response = open_response(0)
        if response.status not in {200, 206}:
            raise TransferError("download server did not return a successful response")
        if current_size and response.status == 206:
            content_range = response.header("Content-Range") or ""
            if not content_range.startswith(f"bytes {current_size}-"):
                raise TransferError("download Range response does not match the partial file")
        received = current_size
        with partial.open("ab" if current_size else "wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
                received += len(chunk)
                if received > expected_size:
                    raise TransferError("download exceeded its declared size")
                if progress:
                    progress(received, expected_size)
    finally:
        response.close()
    if partial.stat().st_size != expected_size:
        raise TransferError("download size does not match its declared size")
    if sha256_file(partial).lower() != expected_sha256.lower():
        raise TransferError("download SHA-256 does not match")
    os.replace(partial, destination)
    return destination


def download_local_output_with_resume(destination: Path, open_response: Callable[[int], BinaryResponse], progress: Callable[[int, int], None] | None = None) -> Path:
    """Resume a localhost output download; its SHA-256 is computed before upload."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    current_size = partial.stat().st_size if partial.exists() else 0
    response = open_response(current_size)
    try:
        if current_size and response.status != 206:
            partial.unlink(missing_ok=True)
            current_size = 0
            response.close()
            response = open_response(0)
        if response.status not in {200, 206}:
            raise TransferError("local VSR output download did not succeed")
        content_range = response.header("Content-Range") or ""
        if response.status == 206:
            if not content_range.startswith(f"bytes {current_size}-") or "/" not in content_range:
                raise TransferError("local VSR output Range response is invalid")
            total_size = int(content_range.rsplit("/", 1)[1])
        else:
            length = response.header("Content-Length")
            if length is None:
                raise TransferError("local VSR output omitted Content-Length")
            total_size = int(length)
        received = current_size
        with partial.open("ab" if current_size else "wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
                received += len(chunk)
                if received > total_size:
                    raise TransferError("local VSR output exceeded its declared size")
                if progress:
                    progress(received, total_size)
    finally:
        response.close()
    if partial.stat().st_size != total_size:
        raise TransferError("local VSR output size is incomplete")
    os.replace(partial, destination)
    return destination


def upload_output_with_resume(relay, claim: ClaimedTask, state: WorkerState, output: Path, video_metadata: dict, progress: Callable[[int, int], None] | None = None) -> str:
    """Upload an output using the relay's idempotent multipart session contract."""
    total_size = output.stat().st_size
    total_sha256 = sha256_file(output)
    session = relay.create_output_upload(claim, total_size, total_sha256, video_metadata)
    if session.part_size_bytes <= 0:
        raise TransferError("relay returned an invalid output part size")
    state.update(claim.task_id, claim.attempt_id, upload_id=session.upload_id, upload_part_size=session.part_size_bytes, status="uploading_output")
    server_parts = dict(session.uploaded_parts)
    completed: list[dict] = []
    sent = 0
    with output.open("rb") as handle:
        part_number = 1
        while chunk := handle.read(session.part_size_bytes):
            offset = sent
            sent += len(chunk)
            part_sha = hashlib.sha256(chunk).hexdigest()
            if part_number in server_parts:
                etag = server_parts[part_number]
            else:
                etag = relay.upload_part(claim, session.upload_id, part_number, f"bytes {offset}-{sent - 1}/{total_size}", part_sha, chunk)
            state.save_upload_part(claim.task_id, claim.attempt_id, part_number, etag, part_sha)
            completed.append({"part_number": part_number, "etag": etag, "sha256": part_sha})
            if progress:
                progress(sent, total_size)
            part_number += 1
    return relay.complete_output_upload(claim, session.upload_id, completed, total_size, total_sha256)
