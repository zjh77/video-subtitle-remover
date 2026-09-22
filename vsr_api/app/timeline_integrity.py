"""Fail closed when subtitle removal changes the video timeline."""

from __future__ import annotations

from fractions import Fraction
from itertools import zip_longest
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


def _timestamp(frame) -> Fraction:
    if frame.pts is None or frame.time_base is None:
        raise RuntimeError("video frame is missing a presentation timestamp")
    return Fraction(frame.pts) * Fraction(frame.time_base)


def _outside_subtitles(frame: np.ndarray, subtitle_areas: Iterable[dict[str, int]]) -> np.ndarray:
    """Return a compact fingerprint made only from pixels the model may not edit."""
    allowed = np.ones(frame.shape[:2], dtype=bool)
    for area in subtitle_areas:
        allowed[area["ymin"]:area["ymax"], area["xmin"]:area["xmax"]] = False
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # A deterministic sparse grid makes a full-video check inexpensive.
    sample = gray[::16, ::16]
    return sample[allowed[::16, ::16]].astype(np.float32)


def _same_picture(before: np.ndarray, after: np.ndarray) -> bool:
    """Allow ordinary lossy-encoding noise without accepting another scene."""
    if before.size != after.size or not before.size:
        return False
    difference = float(np.mean(np.abs(before - after)))
    before_std, after_std = float(np.std(before)), float(np.std(after))
    # Flat frames have no meaningful correlation coefficient.
    if before_std < 2 or after_std < 2:
        return difference <= 12
    correlation = float(np.corrcoef(before, after)[0, 1])
    # Re-encoding a moving or high-detail frame can exceed a fixed per-pixel
    # error budget, while its spatial structure remains nearly identical.
    # A frame from another scene does not retain this correlation.
    return correlation >= 0.985 or (correlation >= 0.96 and difference <= 20)


def validate_timeline(input_path: Path, output_path: Path, subtitle_areas: Iterable[dict[str, int]]) -> None:
    """Verify ordered frames, presentation timestamps, and untouched pixels.

    The check is intentionally performed before an output asset receives an ID;
    a readable MP4 with the right total duration is not sufficient evidence that
    it belongs to the submitted input timeline.
    """
    try:
        import av
    except ImportError as exc:  # pragma: no cover - requirements install PyAV
        raise RuntimeError("PyAV is required for timeline validation.") from exc

    source = av.open(str(input_path))
    result = av.open(str(output_path))
    try:
        source_frames = source.decode(source.streams.video[0])
        result_frames = result.decode(result.streams.video[0])
        index = 0
        missing = object()
        for before, after in zip_longest(source_frames, result_frames, fillvalue=missing):
            if before is missing:
                raise RuntimeError(f"output contains extra frames after frame {index}")
            if after is missing:
                raise RuntimeError(f"output ended before input at frame {index}")
            before_time, after_time = _timestamp(before), _timestamp(after)
            if before_time != after_time:
                raise RuntimeError(
                    f"timeline timestamp mismatch at frame {index}: {before_time} != {after_time}"
                )
            before_pixels = _outside_subtitles(before.to_ndarray(format="bgr24"), subtitle_areas)
            after_pixels = _outside_subtitles(after.to_ndarray(format="bgr24"), subtitle_areas)
            if not _same_picture(before_pixels, after_pixels):
                raise RuntimeError(f"timeline picture mismatch outside subtitle area at frame {index}")
            index += 1
    finally:
        source.close()
        result.close()
