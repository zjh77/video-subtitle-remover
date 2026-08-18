from __future__ import annotations

import multiprocessing as mp
import queue
import subprocess
import threading
from shutil import copy2
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2

from . import db
from .runner import run_vsr
from .service_logging import log_event


def now() -> str: return datetime.now(timezone.utc).isoformat()


class JobQueue:
    def __init__(self):
        self.pending: queue.Queue[str] = queue.Queue(); self.worker = None; self.stop_event = threading.Event(); self.lock = threading.Lock(); self.current_id = None; self.process = None
    def start(self):
        if self.worker and self.worker.is_alive(): return
        self.stop_event.clear(); self.worker = threading.Thread(target=self._loop, daemon=True, name="vsr-api-worker"); self.worker.start()
        for job_id in db.recover_jobs(): self.submit(job_id)
    def stop(self):
        self.stop_event.set(); self.pending.put(""); self._terminate_current()
    def submit(self, job_id: str): self.pending.put(job_id)
    def cancel(self, job_id: str) -> str:
        job = db.get_job(job_id)
        if job["status"] == "queued": db.update_job(job_id, status="cancelled", stage="cancelled", completed_at=now()); db.append_log(job_id, now(), "info", "Cancelled before execution."); return "cancelled"
        with self.lock:
            if self.current_id == job_id: db.update_job(job_id, status="cancelling", stage="cancelling", message="Stopping VSR process."); self._terminate_current(); return "cancelling"
        return job["status"]
    def _terminate_current(self):
        process = self.process
        if process and process.is_alive():
            if process.pid and __import__('os').name == 'nt': subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else: process.terminate()
    def _loop(self):
        while not self.stop_event.is_set():
            job_id = self.pending.get()
            if not job_id: continue
            job = db.get_job(job_id)
            if not job or job["status"] != "queued": continue
            self._run(job)
    def _run(self, job: dict):
        import json
        asset = db.get_asset(job["input_asset_id"]); paths = __import__('vsr_api.app.storage', fromlist=['job_paths']).job_paths(job["id"])
        output = paths["work"] / f"{Path(asset['filename']).stem}_no_sub.mp4"; events = mp.Queue()
        payload = {"input_path": asset["path"], "output_path": str(output), "inpaint_mode": job["inpaint_mode"], "subtitle_areas": json.loads(job["subtitle_areas"])}
        db.update_job(job["id"], status="running", stage="starting", started_at=now(), progress=0)
        log_event(20, "local_job_started", job_id=job["id"])
        with self.lock:
            self.current_id = job["id"]; self.process = mp.Process(target=run_vsr, args=(payload, events), daemon=True); self.process.start()
        while self.process.is_alive(): self._drain(job["id"], events); self.process.join(.25)
        self._drain(job["id"], events)
        current = db.get_job(job["id"])
        if current["status"] == "cancelling": db.update_job(job["id"], status="cancelled", stage="cancelled", completed_at=now()); db.append_log(job["id"], now(), "info", "Task cancelled.")
        elif current["status"] == "running":
            if output.exists():
                try:
                    output_asset_id = self._save_output_asset(job["id"], output)
                    db.update_job(job["id"], status="succeeded", stage="completed", progress=100, completed_at=now(), message="Completed.", output_asset_id=output_asset_id)
                    log_event(20, "local_job_completed", job_id=job["id"])
                except Exception as exc:
                    db.append_log(job["id"], now(), "error", f"Could not save output asset: {exc}")
                    db.update_job(job["id"], status="failed", stage="failed", completed_at=now(), error_code="VSR_OUTPUT_INVALID", message="VSR output could not be validated or saved.")
                    log_event(40, "local_job_output_invalid", job_id=job["id"], error=exc)
            else:
                db.update_job(job["id"], status="failed", stage="failed", completed_at=now(), error_code="VSR_PROCESS_FAILED", message="Subtitle cleanup failed; see task logs.")
                log_event(40, "local_job_process_failed", job_id=job["id"])
        with self.lock: self.current_id = self.process = None
    def _drain(self, job_id, events):
        while True:
            try: event = events.get_nowait()
            except queue.Empty: return
            if event[0] == "log": db.append_log(job_id, now(), event[1], event[2])
            elif event[0] == "progress": db.update_job(job_id, progress=round(float(event[1]), 2), stage=event[2], message=f"Processing: {float(event[1]):.0f}%")
            elif event[0] == "error":
                db.append_log(job_id, now(), "error", event[1]); db.append_log(job_id, now(), "debug", event[2])
                log_event(40, "local_job_runner_error", job_id=job_id, error=event[1], traceback=event[2])

    def _save_output_asset(self, job_id: str, output: Path) -> str:
        from .storage import asset_path
        cap = cv2.VideoCapture(str(output))
        try:
            width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps, frames = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        finally:
            cap.release()
        if width <= 0 or height <= 0 or fps <= 0:
            raise RuntimeError("VSR output is not a readable video.")
        asset_id = f"asset_{uuid4().hex}"; destination = asset_path(asset_id, output.name); copy2(output, destination)
        db.insert("assets", {"id":asset_id, "filename":destination.name, "path":str(destination), "size_bytes":destination.stat().st_size, "width":width, "height":height, "fps":round(fps, 3), "duration_seconds":round(frames / fps, 3), "created_at":now(), "source_job_id":job_id})
        return asset_id


job_queue = JobQueue()
