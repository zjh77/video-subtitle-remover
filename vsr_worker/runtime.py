"""Single-concurrency Worker orchestration and local VSR lifecycle."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cv2

from .config import WorkerSettings
from .local_vsr import LocalVsrClient, LocalVsrError
from .models import ClaimedTask
from .redact import redact_text
from .relay_client import LeaseLostError, RelayClient, RelayError
from .state import WorkerState
from .transfer import TransferError, available_bytes, download_local_output_with_resume, download_with_resume, upload_output_with_resume


LOG = logging.getLogger("vsr_worker")
TERMINAL = {"succeeded", "failed", "cancelled"}


class WorkerRuntime:
    def __init__(self, settings: WorkerSettings):
        self.settings = settings
        self.state = WorkerState(settings.runtime.state_dir)
        self.relay = RelayClient(settings.relay)
        self.local_vsr = LocalVsrClient(settings.runtime.local_vsr_base_url)
        self.stopping = False

    def preflight(self) -> dict[str, Any]:
        self.state.initialize()
        self._sweep_terminal_attempts()
        if available_bytes(self.settings.runtime.state_dir) < self.settings.runtime.min_free_bytes:
            raise RuntimeError("worker state disk space is below its configured minimum")
        health = self.local_vsr.health()
        if health.get("status") != "ok" or not health.get("vsr_available"):
            raise RuntimeError("local VSR health check is not ready")
        return health

    def capabilities(self, health: dict[str, Any]) -> dict[str, Any]:
        return {
            "max_concurrency": 1,
            "supported_inpaint_modes": health.get("supported_inpaint_modes", []),
            "vsr_version": health.get("version", "unknown"),
        }

    def run_forever(self) -> None:
        health = self.preflight()
        capabilities = self.capabilities(health)
        self.relay.register(capabilities)
        self._recover_unfinished()
        while not self.stopping:
            if available_bytes(self.settings.runtime.state_dir) < self.settings.runtime.min_free_bytes:
                LOG.warning("Worker is not claiming tasks because disk space is low.")
                time.sleep(self.settings.runtime.heartbeat_seconds)
                continue
            try:
                claim = self.relay.claim(self.settings.runtime.claim_timeout_seconds, capabilities)
                if claim:
                    self._process(claim)
                else:
                    self.relay.heartbeat()
            except RelayError as exc:
                LOG.warning("Relay communication failed: %s", redact_text(exc))
                time.sleep(self.settings.runtime.heartbeat_seconds)

    def stop(self) -> None:
        self.stopping = True

    def _recover_unfinished(self) -> None:
        """Resume only an attempt which the relay still assigns to this Worker."""
        for record in self.state.unfinished():
            try:
                claim = self.relay.get_attempt(record.task_id, record.attempt_id)
                self.state.save_claim(claim)
                self._process(claim)
            except LeaseLostError:
                self._cleanup_attempt(record.task_id, record.attempt_id, remove_state=True)
            except RelayError as exc:
                LOG.warning("Could not recover a persisted attempt: %s", redact_text(exc))

    def _process(self, claim: ClaimedTask) -> None:
        self.state.save_claim(claim)
        work_dir = self._work_dir(claim)
        input_path = work_dir / "input.mp4"
        output_path = work_dir / "output.mp4"
        controller = _LeaseController(self, claim)
        record = self.state.get(claim.task_id, claim.attempt_id)
        input_asset_id: str | None = record.local_input_asset_id if record else None
        output_asset_id: str | None = record.local_output_asset_id if record else None
        try:
            record = self.state.get(claim.task_id, claim.attempt_id)
            local_job_id = record.local_vsr_job_id if record else None
            if not local_job_id:
                self.state.update(claim.task_id, claim.attempt_id, status="downloading_input")
                self._progress(controller, "downloading_input", 0, "Downloading input video.")
                download_with_resume(
                    input_path,
                    claim.input_asset.size_bytes,
                    claim.input_asset.sha256,
                    lambda start: self.relay.open_input(claim.input_asset.download_url, start),
                    lambda sent, total: self._transfer_progress(controller, "downloading_input", sent, total),
                )
                controller.assert_active()
                if not input_asset_id:
                    self.state.update(claim.task_id, claim.attempt_id, status="uploading_input")
                    self._progress(controller, "uploading_input", 0, "Sending input to local VSR.")
                    input_asset_id = self.local_vsr.upload_asset(input_path, lambda sent, total: self._transfer_progress(controller, "uploading_input", sent, total))
                    self.state.update(claim.task_id, claim.attempt_id, local_input_asset_id=input_asset_id)
                    controller.assert_active()
                local_job_id = self.local_vsr.create_job(input_asset_id, claim.cleanup.subtitle_areas, claim.cleanup.inpaint_mode)
                self.state.update(claim.task_id, claim.attempt_id, status="running_vsr", local_vsr_job_id=local_job_id)
            job = self._wait_for_local_job(claim, controller, local_job_id)
            if job.get("status") == "cancelled":
                self.relay.cancel_task(claim)
                self.state.update(claim.task_id, claim.attempt_id, status="cancelled")
                if input_asset_id:
                    self._delete_local_asset(input_asset_id)
                if output_asset_id:
                    self._delete_local_asset(output_asset_id)
                self._cleanup_attempt(claim.task_id, claim.attempt_id, remove_state=True)
                return
            if job.get("status") != "succeeded":
                raise LocalVsrError("local VSR finished without success")
            output_asset_id = str(job["output_asset_id"])
            self.state.update(claim.task_id, claim.attempt_id, local_output_asset_id=output_asset_id)

            self.state.update(claim.task_id, claim.attempt_id, status="downloading_output")
            self._progress(controller, "downloading_output", 0, "Downloading local VSR output.")
            download_local_output_with_resume(output_path, lambda start: self.local_vsr.open_output(output_asset_id, start), lambda sent, total: self._transfer_progress(controller, "downloading_output", sent, total))
            controller.assert_active()
            metadata = self._video_info(output_path)

            self._progress(controller, "uploading_output", 0, "Uploading output video.")
            remote_output_asset_id = upload_output_with_resume(self.relay, claim, self.state, output_path, metadata, lambda sent, total: self._transfer_progress(controller, "uploading_output", sent, total))
            controller.assert_active()
            self.relay.complete_task(claim, remote_output_asset_id)
            self.state.update(claim.task_id, claim.attempt_id, status="succeeded")
            if input_asset_id:
                self._delete_local_asset(input_asset_id)
            if output_asset_id:
                self._delete_local_asset(output_asset_id)
            self._cleanup_attempt(claim.task_id, claim.attempt_id, remove_state=False, retain_diagnostics=True)
            self._cleanup_attempt(claim.task_id, claim.attempt_id, remove_state=True)
        except LeaseLostError:
            # A fenced-out worker must never report success.  Local files are retained
            # only until the normal failed-attempt sweep can remove diagnostics.
            self.state.update(claim.task_id, claim.attempt_id, status="lease_lost")
            self._write_diagnostic(claim, "Lease lost; terminal report was fenced out.")
            if input_asset_id:
                self._delete_local_asset(input_asset_id)
            if output_asset_id:
                self._delete_local_asset(output_asset_id)
        except Exception as exc:
            message = redact_text(exc)
            # Do not emit tracebacks here: Python tracebacks can contain local paths.
            LOG.error("Task execution failed: %s", message)
            try:
                if controller.cancel_requested:
                    self.relay.cancel_task(claim)
                    self.state.update(claim.task_id, claim.attempt_id, status="cancelled")
                elif controller.active:
                    self.relay.fail_task(claim, "WORKER_EXECUTION_FAILED", message)
                    self.state.update(claim.task_id, claim.attempt_id, status="failed")
            except RelayError as report_exc:
                LOG.warning("Could not report terminal task state: %s", redact_text(report_exc))
            if input_asset_id:
                self._delete_local_asset(input_asset_id)
            if output_asset_id:
                self._delete_local_asset(output_asset_id)
            self._write_diagnostic(claim, f"Task failed: {message}")
            self._cleanup_attempt(claim.task_id, claim.attempt_id, remove_state=False, retain_diagnostics=True)

    def _wait_for_local_job(self, claim: ClaimedTask, controller: "_LeaseController", job_id: str) -> dict[str, Any]:
        after = 0
        while True:
            controller.tick("inpainting")
            if controller.cancel_requested:
                self.local_vsr.cancel(job_id)
            logs = self.local_vsr.get_logs(job_id, after)
            after = int(logs.get("next_after", after))
            items = []
            for item in logs.get("items", []):
                record = self.state.get(claim.task_id, claim.attempt_id)
                sequence = (record.log_sequence if record else 0) + 1
                self.state.update(claim.task_id, claim.attempt_id, log_sequence=sequence)
                items.append({"sequence": sequence, "level": str(item.get("level", "info")), "message": redact_text(item.get("message", ""))})
            if items:
                self.relay.report_logs(claim, items)
            job = self.local_vsr.get_job(job_id)
            self._progress(controller, str(job.get("stage", "inpainting")), job.get("progress"), redact_text(job.get("message", "Processing local VSR task.")))
            if job.get("status") in TERMINAL:
                return job
            time.sleep(2)

    def _progress(self, controller: "_LeaseController", stage: str, progress: float | None, message: str) -> None:
        controller.tick(stage)
        if controller.active:
            self.relay.report_progress(controller.claim, stage, progress, redact_text(message))

    def _transfer_progress(self, controller: "_LeaseController", stage: str, sent: int, total: int) -> None:
        self._progress(controller, stage, (sent / total * 100) if total else None, f"{stage}: {sent}/{total} bytes")

    def _work_dir(self, claim: ClaimedTask) -> Path:
        # IDs originate from the relay, so discard all path syntax defensively.
        safe_task = "".join(ch for ch in claim.task_id if ch.isalnum() or ch in "-_")
        safe_attempt = "".join(ch for ch in claim.attempt_id if ch.isalnum() or ch in "-_")
        directory = self.settings.runtime.state_dir / "tasks" / safe_task / safe_attempt
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _cleanup_attempt(self, task_id: str, attempt_id: str, remove_state: bool, retain_diagnostics: bool = False) -> None:
        directory = self.settings.runtime.state_dir / "tasks" / "".join(ch for ch in task_id if ch.isalnum() or ch in "-_") / "".join(ch for ch in attempt_id if ch.isalnum() or ch in "-_")
        if retain_diagnostics:
            for pattern in ("*.mp4", "*.part"):
                for item in directory.glob(pattern):
                    item.unlink(missing_ok=True)
        else:
            shutil.rmtree(directory, ignore_errors=True)
        if remove_state:
            self.state.remove(task_id, attempt_id)

    def _write_diagnostic(self, claim: ClaimedTask, message: str) -> None:
        with (self._work_dir(claim) / "diagnostic.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()} {redact_text(message)}\n")

    def _sweep_terminal_attempts(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        for record in self.state.terminal_before(cutoff):
            self._cleanup_attempt(record.task_id, record.attempt_id, remove_state=True)

    def _delete_local_asset(self, asset_id: str) -> None:
        try:
            self.local_vsr.delete_asset(asset_id)
        except LocalVsrError as exc:
            LOG.warning("Could not delete a local VSR asset: %s", redact_text(exc))

    @staticmethod
    def _video_info(path: Path) -> dict[str, Any]:
        capture = cv2.VideoCapture(str(path))
        try:
            width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps, frames = capture.get(cv2.CAP_PROP_FPS), capture.get(cv2.CAP_PROP_FRAME_COUNT)
        finally:
            capture.release()
        if width <= 0 or height <= 0 or fps <= 0:
            raise TransferError("local VSR output is not a readable video")
        return {"width": width, "height": height, "fps": round(fps, 3), "duration_seconds": round(frames / fps, 3)}


class _LeaseController:
    def __init__(self, runtime: WorkerRuntime, claim: ClaimedTask):
        self.runtime = runtime
        self.claim = claim
        self.active = True
        self.cancel_requested = False
        self._deadline = time.monotonic() + runtime.settings.runtime.lease_seconds
        self._last_heartbeat = 0.0
        self._last_renew = 0.0

    def tick(self, stage: str) -> None:
        now = time.monotonic()
        if now >= self._deadline:
            self.active = False
            raise LeaseLostError("local lease deadline elapsed")
        current = {"task_id": self.claim.task_id, "attempt_id": self.claim.attempt_id, "stage": stage}
        try:
            if now - self._last_heartbeat >= self.runtime.settings.runtime.heartbeat_seconds:
                heartbeat = self.runtime.relay.heartbeat(current)
                self.cancel_requested = self.cancel_requested or heartbeat.cancel_requested
                self._last_heartbeat = now
            if now - self._last_renew >= self.runtime.settings.runtime.lease_renew_seconds:
                renewal = self.runtime.relay.renew_lease(self.claim)
                self.cancel_requested = self.cancel_requested or renewal.cancel_requested
                self._last_renew = now
                self._deadline = now + self.runtime.settings.runtime.lease_seconds
        except RelayError:
            # A transient outage is tolerated only until the persisted lease deadline.
            if time.monotonic() >= self._deadline:
                self.active = False
                raise LeaseLostError("relay lease could not be renewed")

    def assert_active(self) -> None:
        self.tick("checking_lease")
        if not self.active:
            raise LeaseLostError("relay lease is no longer active")
