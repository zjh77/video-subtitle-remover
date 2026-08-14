from __future__ import annotations
import json, shutil
from pathlib import Path
from .models import ClaimedLease,InputArtifact,SubtitleCleanupOptions
from .relay_client import LeaseLostError
from .transfer import download_with_resume,upload_output_with_resume,sha256_file
class WorkerRuntime:
 def __init__(self,settings,relay=None,local_vsr=None,state=None):
  from .relay_client import RelayClient
  from .local_vsr import LocalVsrClient
  from .state import WorkerState
  self.settings=settings;self.relay=relay or RelayClient(settings.relay);self.local_vsr=local_vsr or LocalVsrClient(settings.runtime.local_vsr_base_url);self.state=state or WorkerState(settings.runtime.state_dir)
 def process(self,lease:ClaimedLease):
  self.state.save_claim(lease); d=self.settings.runtime.state_dir/'tasks'/lease.job_id/lease.lease_id; d.mkdir(parents=True,exist_ok=True); src=d/'input.mp4';out=d/'output.mp4'; input_id=output_id=None
  try:
   download_with_resume(src,lease.input_artifact.size_bytes,lease.input_artifact.sha256,lambda n:self.relay.open_input(lease.input_artifact.download_url,n))
   input_id=self.local_vsr.upload_asset(src); job=self.local_vsr.create_job(input_id,lease.cleanup.subtitle_areas,lease.cleanup.inpaint_mode);self.state.update(lease.lease_id,local_input_asset_id=input_id,local_vsr_job_id=job,status='running')
   status=self.local_vsr.get_job(job)
   if status.get('status')=='cancelled':self.relay.cancel_task(lease);return
   output_id=str(status['output_asset_id']); self.local_vsr.download_output(output_id,out)
   digest=upload_output_with_resume(self.relay,lease,self.state,out);self.relay.complete_task(lease,digest);self.state.update(lease.lease_id,status='succeeded')
  except LeaseLostError: self.state.update(lease.lease_id,status='lease_lost');raise
  finally:
   for a in (input_id,output_id):
    if a:
     try:self.local_vsr.delete_asset(a)
     except Exception:pass
   shutil.rmtree(d,ignore_errors=True)
