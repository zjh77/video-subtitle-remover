import hashlib
import json
import shutil
import unittest
from http.client import RemoteDisconnected
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from vsr_worker.models import ClaimedLease
from vsr_worker.local_vsr import LocalVsrTransientError
from vsr_worker.observability import JsonlAuditLogger
from vsr_worker.relay_client import LeaseLostError, RelayClient, RelayProtocolError, RelayTransientError
from vsr_worker.runtime import WorkerRuntime
from vsr_worker.state import WorkerState
from vsr_worker.transfer import download_with_resume, upload_output_with_resume


def lease(cancel_requested=False):
    return ClaimedLease.from_response({"lease_id":"att_1","job_id":"vcj_1","attempt":1,"lease_expires_at":"future","cancel_requested":cancel_requested,"trace_id":"trace_1","client_request_id":"request_1","operation":{"type":"subtitle_cleanup","options":{"subtitle_areas":[{"ymin":0,"ymax":1,"xmin":0,"xmax":1}],"inpaint_mode":"opencv"}},"input":{"artifact_id":"art_1","size_bytes":3,"sha256":hashlib.sha256(b"abc").hexdigest(),"download_url":"/api/v1/artifacts/art_1/download?token=secret"}})


class Response:
    def __init__(self, offset):
        self.status=206 if offset else 200; self.body=BytesIO(b"abc"[offset:]); self.headers={"Content-Length":str(3-offset)}
        if offset: self.headers["Content-Range"]=f"bytes {offset}-2/3"
    def read(self, size=-1): return self.body.read(size)
    def close(self): pass


class FakeRelay:
    def __init__(self, *, cancel_on_renew=False, cancel_on_heartbeat=False, lose_on_complete=False):
        self.cancel_on_renew=cancel_on_renew; self.cancel_on_heartbeat=cancel_on_heartbeat; self.lose_on_complete=lose_on_complete
        self.parts={}; self.completed=False; self.cancelled=False; self.failed=[]; self.renewals=0; self.heartbeats=0; self.recovered=[]
    def heartbeat(self, *_):
        self.heartbeats += 1
        return SimpleNamespace(cancel_lease_ids=frozenset({"att_1"}) if self.cancel_on_heartbeat else frozenset())
    def renew_lease(self, _): self.renewals += 1; return "future", self.cancel_on_renew
    def open_input(self, _, offset): return Response(offset)
    def report_log(self, *_): pass
    def report_progress(self, *_): pass
    def create_output_upload(self, *_): return SimpleNamespace(upload_id="upl_1", part_size_bytes=2)
    def get_upload_parts(self, _): return self.parts
    def upload_part(self, _, number, __, digest, ___): self.parts[number]=digest; return digest
    def complete_upload(self, *_): pass
    def complete_task(self, *_):
        if self.lose_on_complete: raise LeaseLostError("lost")
        self.completed=True
    def cancel_task(self, *_): self.cancelled=True
    def fail_task(self, _, code, message): self.failed.append((code,message))
    def recover_lease(self, lease_id): self.recovered.append(lease_id); return lease()


class FakeLocal:
    def __init__(self): self.created=0; self.cancelled=[]; self.deleted=[]
    def upload_asset(self, *_): return "input_asset"
    def health(self): return {"status":"ok"}
    def create_job(self, *_): self.created += 1; return "local_job"
    def get_logs(self, *_): return {"next_after":0,"items":[]}
    def get_job(self, *_): return {"status":"succeeded","output_asset_id":"output_asset","progress":100}
    def download_output(self, _, path): path.write_bytes(b"abc")
    def cancel(self, job_id): self.cancelled.append(job_id)
    def delete_asset(self, asset_id): self.deleted.append(asset_id)


class WorkerTests(unittest.TestCase):
    def runtime_dir(self):
        path=Path.cwd()/"tests"/f".runtime-test-state-{uuid4().hex}"
        path.mkdir(parents=True); (path/".write-probe").write_text("ok")
        return path
    def setup_runtime(self, relay=None, local=None):
        root=self.runtime_dir(); state=WorkerState(root); state.initialize()
        settings=SimpleNamespace(runtime=SimpleNamespace(state_dir=root,lease_renew_seconds=1,heartbeat_seconds=1,min_free_bytes=0,claim_timeout_seconds=20,relay_reconnect_initial_seconds=1,relay_reconnect_max_seconds=4),relay=SimpleNamespace(worker_id="worker_1"))
        runtime=WorkerRuntime(settings,relay or FakeRelay(),local or FakeLocal(),state)
        self.addCleanup(shutil.rmtree,root,True)
        self.addCleanup(runtime.audit.close)
        return root,state,runtime
    def test_claim_download_parts_complete(self):
        root=self.runtime_dir()
        try:
            state=WorkerState(root); state.initialize(); claimed=lease(); state.save_claim(claimed)
            self.assertEqual(download_with_resume(root/"in.mp4",3,claimed.input_artifact.sha256,lambda n:Response(n)).read_bytes(),b"abc")
            output=root/"out.mp4"; output.write_bytes(b"abcdef"); relay=FakeRelay()
            self.assertEqual(upload_output_with_resume(relay,claimed,state,output),hashlib.sha256(b"abcdef").hexdigest())
            self.assertEqual(set(relay.parts),{0,1,2})
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_truncated_input_download_is_recoverable_and_resumes(self):
        class TruncatedResponse(Response):
            def __init__(self): self.status=200; self.body=BytesIO(b"a"); self.headers={"Content-Length":"3"}
        root=self.runtime_dir()
        try:
            claimed=lease(); destination=root/"input.mp4"
            with self.assertRaises(RelayTransientError): download_with_resume(destination,3,claimed.input_artifact.sha256,lambda _:TruncatedResponse())
            self.assertEqual(destination.with_suffix(".mp4.part").read_bytes(),b"a")
            self.assertEqual(download_with_resume(destination,3,claimed.input_artifact.sha256,lambda offset:Response(offset)).read_bytes(),b"abc")
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_download_rejects_incorrect_content_range(self):
        class BadRange(Response):
            def __init__(self):
                super().__init__(1); self.headers["Content-Range"]="bytes 0-2/3"
        root=self.runtime_dir()
        try:
            part=root/"input.mp4.part"; part.parent.mkdir(parents=True,exist_ok=True); part.write_bytes(b"a")
            with self.assertRaises(RelayProtocolError) as error: download_with_resume(root/"input.mp4",3,lease().input_artifact.sha256,lambda _:BadRange())
            self.assertIn("Content-Range",str(error.exception))
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_operation_rejects_missing_options(self):
        with self.assertRaises(ValueError): ClaimedLease.from_response({"lease_id":"a","job_id":"j","attempt":1,"lease_expires_at":"x","operation":{"type":"subtitle_cleanup"},"input":{"artifact_id":"i","size_bytes":1,"sha256":"a"*64,"download_url":"/"}})
    def test_runtime_success(self):
        root,state,runtime=self.setup_runtime()
        try:
            runtime.process(lease())
            self.assertTrue(runtime.relay.completed); self.assertEqual(state.get("att_1").status,"succeeded")
            self.assertEqual(runtime.local_vsr.created,1); self.assertFalse((root/"tasks"/"vcj_1"/"att_1").exists())
            self.assertGreaterEqual(runtime.relay.heartbeats,1)
            lines=[json.loads(line) for line in (root/"logs"/"worker.jsonl").read_text(encoding="utf-8").splitlines()]
            completed=next(line for line in lines if line["event"]=="task_completed")
            self.assertEqual(completed["trace_id"],"trace_1"); self.assertEqual(completed["client_request_id"],"request_1")
            downloaded=next(line for line in lines if line["event"]=="input_download_completed")
            uploaded=next(line for line in lines if line["event"]=="output_upload_completed")
            self.assertEqual(downloaded["size_bytes"],3); self.assertEqual(uploaded["size_bytes"],3)
            self.assertIn("throughput_mbps",downloaded); self.assertIn("elapsed_ms",uploaded)
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_runtime_cancel_stops_upload_and_reports_cancelled(self):
        root,state,runtime=self.setup_runtime(FakeRelay(cancel_on_renew=True))
        try:
            state.save_claim(lease()); state.update("att_1",local_input_asset_id="input_asset",local_vsr_job_id="local_job",status="running")
            runtime.process(lease())
            self.assertTrue(runtime.relay.cancelled); self.assertFalse(runtime.relay.completed)
            self.assertEqual(runtime.local_vsr.cancelled,["local_job"]); self.assertEqual(state.get("att_1").status,"cancelled")
            self.assertFalse((root/"tasks"/"vcj_1"/"att_1").exists())
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_heartbeat_cancel_stops_local_job(self):
        root,state,runtime=self.setup_runtime(FakeRelay(cancel_on_heartbeat=True))
        try:
            state.save_claim(lease()); state.update("att_1",local_input_asset_id="input_asset",local_vsr_job_id="local_job",status="running")
            runtime.process(lease())
            self.assertTrue(runtime.relay.cancelled); self.assertEqual(runtime.local_vsr.cancelled,["local_job"])
            self.assertEqual(state.get("att_1").status,"cancelled")
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_runtime_lease_loss_never_completes(self):
        root,state,runtime=self.setup_runtime(FakeRelay(lose_on_complete=True))
        try:
            runtime.process(lease())
            self.assertFalse(runtime.relay.completed); self.assertEqual(state.get("att_1").status,"lease_lost")
            self.assertFalse((root/"tasks"/"vcj_1"/"att_1").exists()); self.assertTrue((root/"diagnostics"/"att_1.log").is_file())
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_recover_active_lease_reuses_local_job(self):
        root,state,runtime=self.setup_runtime()
        try:
            state.save_claim(lease()); state.update("att_1",local_input_asset_id="input_asset",local_vsr_job_id="local_job",status="running")
            runtime.recover_unfinished()
            self.assertEqual(runtime.relay.recovered,["att_1"]); self.assertEqual(runtime.local_vsr.created,0)
            self.assertTrue(runtime.relay.completed); self.assertEqual(state.get("att_1").status,"succeeded")
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_recover_expired_lease_fences_without_creating_job(self):
        class ExpiredRelay(FakeRelay):
            def recover_lease(self, _): raise LeaseLostError("expired")
        root,state,runtime=self.setup_runtime(ExpiredRelay())
        try:
            state.save_claim(lease()); state.update("att_1",local_input_asset_id="input_asset",local_vsr_job_id="local_job",status="running")
            runtime.recover_unfinished()
            self.assertEqual(state.get("att_1").status,"lease_lost"); self.assertEqual(runtime.local_vsr.created,0)
            self.assertFalse(runtime.relay.completed); self.assertFalse((root/"tasks"/"vcj_1"/"att_1").exists())
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_jsonl_logger_redacts_sensitive_values(self):
        root=self.runtime_dir()
        try:
            logger=JsonlAuditLogger(root/"audit",1024,1)
            logger.emit("INFO","request",authorization="Bearer token-value",download_url="https://example.invalid/file?signature=secret",path="C:\\sensitive\\video.mp4",unix_path="/private/video.mp4",message="ok")
            logger.emit("INFO","heartbeat",job_id="job_1")
            logger.emit("INFO","input_download_completed",job_id="job_1",size_bytes=3,elapsed_ms=2,throughput_mbps=12.0,resumed=False)
            text=(root/"audit"/"worker.jsonl").read_text(encoding="utf-8")
            summary=(root/"audit"/"worker-summary.log").read_text(encoding="utf-8")
            self.assertIn('"event":"request"',text); self.assertIn('[REDACTED]',text); self.assertIn('[LOCAL_PATH]',text)
            self.assertNotIn("token-value",text); self.assertNotIn("signature=secret",text); self.assertNotIn("sensitive\\video",text)
            self.assertNotIn("private/video",text)
            self.assertIn("input_download_completed",summary); self.assertNotIn("heartbeat",summary)
        finally: logger.close(); shutil.rmtree(root,ignore_errors=True)
    def test_run_forever_reconnects_after_transient_register_failure(self):
        class Audit:
            def __init__(self): self.events=[]
            def emit(self, level, event, **fields): self.events.append((level,event,fields))
        class ReconnectingRelay:
            def __init__(self): self.register_calls=0
            def register(self, *_):
                self.register_calls += 1
                if self.register_calls == 1: raise RelayTransientError("network unavailable")
            def heartbeat(self, *_): return SimpleNamespace(cancel_lease_ids=frozenset())
            def claim(self, _): raise KeyboardInterrupt()
        root,state,_=self.setup_runtime(); delays=[]; audit=Audit(); relay=ReconnectingRelay()
        settings=SimpleNamespace(runtime=SimpleNamespace(state_dir=root,lease_renew_seconds=1,heartbeat_seconds=1,min_free_bytes=0,claim_timeout_seconds=20,relay_reconnect_initial_seconds=1,relay_reconnect_max_seconds=4),relay=SimpleNamespace(worker_id="worker_1"))
        runtime=WorkerRuntime(settings,relay,FakeLocal(),state,audit,sleep=delays.append)
        try:
            with self.assertRaises(KeyboardInterrupt): runtime.run_forever()
            self.assertEqual(relay.register_calls,2); self.assertEqual(delays,[1])
            self.assertIn("reconnect_wait",[event for _,event,_ in audit.events]); self.assertIn("reconnected",[event for _,event,_ in audit.events])
        finally: shutil.rmtree(root,ignore_errors=True)
    def test_transient_lease_failure_preserves_recovery_state(self):
        class DroppedRelay(FakeRelay):
            def open_input(self, *_): raise RelayTransientError("network unavailable")
        root,state,runtime=self.setup_runtime(DroppedRelay())
        try:
            with self.assertRaises(RelayTransientError): runtime.process(lease())
            self.assertEqual(state.get("att_1").status,"claimed")
            self.assertTrue((root/"tasks"/"vcj_1"/"att_1").exists())
        finally: shutil.rmtree(root,ignore_errors=True)

    def test_local_vsr_poll_disconnect_retries_existing_job(self):
        class FlakyLocal(FakeLocal):
            def __init__(self):
                super().__init__(); self.log_calls=0
            def get_logs(self, *_):
                self.log_calls += 1
                if self.log_calls == 1:
                    raise LocalVsrTransientError("local VSR API request failed")
                return {"next_after":0,"items":[]}

        root=self.runtime_dir(); state=WorkerState(root); state.initialize(); delays=[]
        settings=SimpleNamespace(runtime=SimpleNamespace(state_dir=root,lease_renew_seconds=1,heartbeat_seconds=2,min_free_bytes=0,claim_timeout_seconds=20,relay_reconnect_initial_seconds=1,relay_reconnect_max_seconds=4),relay=SimpleNamespace(worker_id="worker_1"))
        runtime=WorkerRuntime(settings,FakeRelay(),FlakyLocal(),state,sleep=delays.append)
        self.addCleanup(runtime.audit.close)
        try:
            runtime.process(lease())
            self.assertTrue(runtime.relay.completed)
            self.assertEqual(runtime.local_vsr.created,1)
            self.assertEqual(runtime.local_vsr.log_calls,2)
            self.assertEqual(delays,[1])
            events=[json.loads(line)["event"] for line in (root/"logs"/"worker.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("local_vsr_retry",events)
        finally: shutil.rmtree(root,ignore_errors=True)

    def test_remote_disconnect_is_reconnectable(self):
        class DisconnectingOpener:
            def open(self, *_args, **_kwargs):
                raise RemoteDisconnected("Remote end closed connection without response")

        client=object.__new__(RelayClient)
        client.origin="https://relay.example"
        client.settings=SimpleNamespace(worker_id="worker_1",worker_token="test-token")
        client.opener=DisconnectingOpener()
        with self.assertRaises(RelayTransientError) as error:
            client._json("POST","/workers/worker_1/register",{})
        self.assertIn("connection failed",str(error.exception))

if __name__=="__main__": unittest.main()
