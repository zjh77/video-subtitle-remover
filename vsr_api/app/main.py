from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from backend.tools.constant import InpaintMode
from . import db
from .config import ensure_data_dirs
from .job_queue import job_queue
from .schemas import JobCreate
from .storage import persist_upload, remove_asset


def now() -> str: return datetime.now(timezone.utc).isoformat()
app = FastAPI(title="Subtitle Cleanup Service", version="1.0.0")


@app.on_event("startup")
def startup(): ensure_data_dirs(); db.init_db(); job_queue.start()
@app.on_event("shutdown")
def shutdown(): job_queue.stop()


@app.get("/health")
def health(): return {"status":"ok", "version":"1.0.0", "vsr_available":True, "supported_inpaint_modes":[item.value for item in InpaintMode], "max_concurrency":1}


@app.post("/api/v1/assets")
async def create_asset(file: UploadFile = File(...)):
    asset_id = f"asset_{uuid4().hex}"; path, size, filename = persist_upload(asset_id, file); video = _video_info(path)
    if video is None: remove_asset(str(path)); raise HTTPException(400, "Uploaded file is not a readable video")
    db.insert("assets", {"id":asset_id,"filename":filename,"path":str(path),"size_bytes":size,**video,"created_at":now(),"source_job_id":None})
    return {"asset_id":asset_id,"filename":filename,"size_bytes":size,"video":video,"created_at":now()}


@app.post("/api/v1/jobs")
def create_job(payload: JobCreate):
    asset = _asset(payload.input_asset_id)
    for area in payload.subtitle_areas:
        if area.xmax > asset["width"] or area.ymax > asset["height"]: raise HTTPException(422, "Subtitle area is outside the input video bounds")
    job_id = f"subclean_{uuid4().hex}"; created = now()
    db.insert("jobs", {"id":job_id,"input_asset_id":asset["id"],"output_asset_id":None,"status":"queued","inpaint_mode":payload.inpaint_mode,"subtitle_areas":json.dumps([area.model_dump() for area in payload.subtitle_areas]),"progress":0,"stage":"queued","message":"Waiting for worker.","error_code":None,"created_at":created,"started_at":None,"completed_at":None})
    db.append_log(job_id, created, "info", "Task queued."); job_queue.submit(job_id)
    return {"job_id":job_id,"status":"queued","created_at":created}


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    job = _job(job_id); result = {"job_id":job["id"], **{key: job[key] for key in ("status","stage","progress","message","created_at","started_at","completed_at") if job.get(key) is not None}}
    if job["status"] == "succeeded":
        asset = _asset(job["output_asset_id"]); result.update(output_asset_id=asset["id"], video={key:asset[key] for key in ("width","height","fps","duration_seconds")})
    if job.get("error_code"): result["error_code"] = job["error_code"]
    return result


@app.get("/api/v1/jobs/{job_id}/logs")
def logs(job_id: str, after: int = 0):
    _job(job_id); items = db.get_logs(job_id, max(after, 0)); return {"next_after":items[-1]["sequence"] if items else max(after,0), "items":items}


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel(job_id: str):
    job = _job(job_id)
    if job["status"] in ("succeeded","failed","cancelled"): return {"job_id":job_id,"status":job["status"]}
    return {"job_id":job_id,"status":job_queue.cancel(job_id)}


@app.get("/api/v1/assets/{asset_id}/download")
def download(asset_id: str):
    asset = _asset(asset_id)
    if not asset["source_job_id"]: raise HTTPException(403, "Only output assets may be downloaded")
    return FileResponse(asset["path"], filename=asset["filename"], media_type="video/mp4")


@app.delete("/api/v1/assets/{asset_id}")
def delete_asset(asset_id: str):
    asset = db.get_asset(asset_id)
    if not asset: return {"status":"not_found"}
    if db.jobs_using_asset(asset_id): raise HTTPException(409, "Asset is in use by an active job")
    remove_asset(asset["path"]); db.delete_asset(asset_id); return {"status":"deleted"}


def _asset(asset_id: str) -> dict:
    asset = db.get_asset(asset_id)
    if not asset: raise HTTPException(404, "Asset not found")
    return asset
def _job(job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job: raise HTTPException(404, "Job not found")
    return job
def _video_info(path: Path) -> dict | None:
    cap = cv2.VideoCapture(str(path))
    try:
        width, height, fps, frames = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if width <= 0 or height <= 0 or fps <= 0: return None
        return {"width":width,"height":height,"fps":round(fps, 3),"duration_seconds":round(frames / fps, 3)}
    finally: cap.release()
