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
            # CRF encoding may alter a few values.  A scene from another point in
            # the timeline is many orders of magnitude farther away than this.
            if before_pixels.size != after_pixels.size or np.mean(np.abs(before_pixels - after_pixels)) > 8:
                raise RuntimeError(f"timeline picture mismatch outside subtitle area at frame {index}")
            index += 1
    finally:
        source.close()
        result.close()
