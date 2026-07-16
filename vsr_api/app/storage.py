from __future__ import annotations

import re
import shutil
from pathlib import Path

from fastapi import UploadFile

from .config import ASSETS_ROOT, JOBS_ROOT, ensure_data_dirs


def sanitize_filename(name: str) -> str:
    source = Path(name).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(source).stem).strip("._-") or "video"
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(source).suffix) or ".mp4"
    return f"{stem}{suffix}"


def asset_path(asset_id: str, filename: str) -> Path:
    ensure_data_dirs()
    root = ASSETS_ROOT / asset_id
    root.mkdir(parents=True, exist_ok=True)
    return root / sanitize_filename(filename)


def job_paths(job_id: str) -> dict[str, Path]:
    ensure_data_dirs()
    root = JOBS_ROOT / job_id
    paths = {"root": root, "work": root / "work", "logs": root / "logs"}
    for path in paths.values(): path.mkdir(parents=True, exist_ok=True)
    return paths


def persist_upload(asset_id: str, upload: UploadFile) -> tuple[Path, int, str]:
    filename = sanitize_filename(upload.filename or "video.mp4")
    destination = asset_path(asset_id, filename)
    size = 0
    with destination.open("wb") as f:
        while chunk := upload.file.read(1024 * 1024):
            f.write(chunk); size += len(chunk)
    return destination, size, filename


def remove_asset(path: str) -> None:
    shutil.rmtree(Path(path).parent, ignore_errors=True)
