from __future__ import annotations

import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any


def now() -> str: return datetime.now(timezone.utc).isoformat()


def run_vsr(payload: dict[str, Any], events) -> None:
    """Runs in a dedicated process so its model state and FFmpeg children are cancellable."""
    try:
        from backend.config import config
        from backend.main import SubtitleRemover
        from backend.tools.constant import InpaintMode
        source, output = Path(payload["input_path"]), Path(payload["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        config.inpaintMode.value = InpaintMode(payload["inpaint_mode"])
        remover = SubtitleRemover(str(source), gui_mode=False)
        remover.video_out_path = str(output)
        remover.sub_areas = [tuple((a["ymin"], a["ymax"], a["xmin"], a["xmax"])) for a in payload["subtitle_areas"]]
        remover.append_output = MethodType(lambda self, *args: events.put(("log", "info", " ".join(map(str, args)))), remover)
        remover.add_progress_listener(lambda progress, finished: events.put(("progress", progress, "completed" if finished else "inpainting")))
        events.put(("log", "info", f"Starting VSR with {payload['inpaint_mode']}."))
        remover.run()
        if not output.exists(): raise RuntimeError("VSR completed without producing an output video.")
        events.put(("success",))
    except Exception as exc:
        events.put(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))
