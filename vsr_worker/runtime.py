"""Single-concurrency, crash-recoverable runtime for formal Worker leases."""
from __future__ import annotations
import shutil, time
from .redact import redact_text
from .observability import JsonlAuditLogger
from .relay_client import LeaseLostError, RelayError, RelayProtocolError, RelayTransientError
from .state import WorkerState
from .transfer import download_with_resume, upload_output_with_resume

class _Cancelled(Exception): pass

class WorkerRuntime:
 def __init__(s,settings,relay=None,local_vsr=None,state=None,audit_logger=None,sleep=None):
  from .relay_client import RelayClient
  from .local_vsr import LocalVsrClient
  s.settings=settings;s.relay=relay or RelayClient(settings.relay);s.local_vsr=local_vsr or LocalVsrClient(settings.runtime.local_vsr_base_url);s.state=state or WorkerState(settings.runtime.state_dir);runtime=settings.runtime;s.audit=audit_logger or JsonlAuditLogger(getattr(runtime,'log_dir',None) or runtime.state_dir/'logs',getattr(runtime,'log_max_bytes',10485760),getattr(runtime,'log_backup_count',7));s.sleep=sleep or time.sleep
 @staticmethod
 def _capabilities(): return {'subtitle_cleanup':True,'max_concurrency':1}
 def run_forever(s):
  s.state.initialize();s._event('INFO','startup_validated');s._ensure_capacity();s.local_vsr.health();s._event('INFO','local_vsr_healthy');attempt=0
  while True:
   try:
    s.relay.register(s.settings.relay.worker_id,s._capabilities());s._event('INFO','worker_registered')
    if attempt:s._event('INFO','reconnected',error_code='RELAY_RECONNECTED',reconnect_attempt=attempt)
    attempt=0;s.recover_unfinished()
    while True:
     heartbeat=s.relay.heartbeat(s._capabilities());s._event('INFO','heartbeat');s._ensure_capacity();lease=s.relay.claim(s.settings.runtime.claim_timeout_seconds)
     if lease:s._event('INFO','lease_claimed',lease);s.process(lease,heartbeat.cancel_lease_ids)
     else:s._event('INFO','claim_empty')
   except LeaseLostError:
    # Authentication/authorization failure is not transient; let the launcher report it.
    raise
   except RelayTransientError as exc:
    attempt+=1;s._wait_to_reconnect(attempt,exc,'RELAY_TRANSIENT')
   except (RelayProtocolError,RelayError) as exc:
    attempt+=1;s._wait_to_reconnect(attempt,exc,'RELAY_PROTOCOL')
 def recover_unfinished(s):
  """Refresh active leases; rejected leases are fenced and never recreated."""
  for record in s.state.unfinished():
   try: lease=s.relay.recover_lease(record.lease_id);s._event('INFO','lease_recovered',lease)
   except LeaseLostError:s._lose(record,'lease recovery rejected');continue
   s.state.save_claim(lease)
   if lease.cancel_requested:s._cancel(lease);continue
   current=s.state.get(lease.lease_id)
   if current and current.local_input_asset_id and not current.local_vsr_job_id:
    s._fail_ambiguous(lease);continue
   s.process(lease)
 def process(s,lease,cancelled_lease_ids=frozenset()):
  started=time.monotonic();s.state.save_claim(lease);directory=s._dir(lease);directory.mkdir(parents=True,exist_ok=True);source=directory/'input.mp4';output=directory/'output.mp4';control=_LeaseControl(s,lease,cancelled_lease_ids);s._event('INFO','task_started',lease,elapsed_ms=0)
  try:
   if lease.cancel_requested:raise _Cancelled()
   control.check(True);record=s.state.get(lease.lease_id)
   if record is None:raise RuntimeError('local lease state was not persisted')
   job_id,input_asset_id=record.local_vsr_job_id,record.local_input_asset_id
   if not job_id:
    if input_asset_id:raise RuntimeError('ambiguous local VSR job after restart')
    s._event('INFO','input_download_started',lease);download_with_resume(source,lease.input_artifact.size_bytes,lease.input_artifact.sha256,lambda offset:s.relay.open_input(lease.input_artifact.download_url,offset),lambda *_:control.check());s._event('INFO','input_verified',lease)
    control.check();s._event('INFO','local_asset_upload_started',lease);input_asset_id=s.local_vsr.upload_asset(source,lambda *_:control.check());s.state.update(lease.lease_id,local_input_asset_id=input_asset_id);s._event('INFO','local_asset_uploaded',lease)
    control.check();s._event('INFO','local_job_create_started',lease);job_id=s.local_vsr.create_job(input_asset_id,lease.cleanup.subtitle_areas,lease.cleanup.inpaint_mode);s.state.update(lease.lease_id,local_vsr_job_id=job_id,status='running');s._event('INFO','local_job_created',lease,local_vsr_job_id=job_id)
   job=s._wait(lease,job_id,control)
   if job.get('status')!='succeeded':raise RuntimeError('local VSR job did not succeed')
   output_asset_id=str(job['output_asset_id']);s.state.update(lease.lease_id,local_output_asset_id=output_asset_id)
   if not output.exists():s._event('INFO','local_output_download_started',lease,local_vsr_job_id=job_id);s.local_vsr.download_output(output_asset_id,output);s._event('INFO','local_output_downloaded',lease,local_vsr_job_id=job_id)
   control.check();s._event('INFO','output_upload_started',lease,local_vsr_job_id=job_id);digest=upload_output_with_resume(s.relay,lease,s.state,output,lambda *_:control.check());s._event('INFO','output_uploaded',lease,local_vsr_job_id=job_id);control.check(True);s.relay.complete_task(lease,digest);s.state.update(lease.lease_id,status='succeeded');s._event('INFO','task_completed',lease,local_vsr_job_id=job_id,elapsed_ms=round((time.monotonic()-started)*1000));s._cleanup(lease)
  except _Cancelled:s._cancel(lease)
  except LeaseLostError:s._lose(s.state.get(lease.lease_id),'lease operation rejected')
  except RelayTransientError as exc:s._event('WARNING','relay_connection_lost',lease,error_code='RELAY_TRANSIENT',error=redact_text(exc));raise
  except RelayError as exc:s._event('WARNING','relay_connection_lost',lease,error_code='RELAY_PROTOCOL',error=redact_text(exc));raise
  except Exception as exc:s._fail(lease,exc)
 def _wait(s,lease,job_id,control):
  record=s.state.get(lease.lease_id);after=record.log_sequence if record else 0
  while True:
   control.check();logs=s.local_vsr.get_logs(job_id,after);after=logs.get('next_after',after)
   for item in logs.get('items',[]):
    record=s.state.get(lease.lease_id)
    if record is None:raise RuntimeError('local lease state disappeared')
    seq=record.log_sequence+1;s.relay.report_log(lease,seq,str(item.get('level','INFO')),redact_text(item.get('message','')));s.state.update(lease.lease_id,log_sequence=seq)
   job=s.local_vsr.get_job(job_id);s.relay.report_progress(lease,job.get('progress'),redact_text(job.get('message','')))
   if job.get('status') in {'succeeded','failed','cancelled'}:return job
   time.sleep(1)
 def _cancel(s,lease):
  record=s.state.get(lease.lease_id)
  try:
   if record and record.local_vsr_job_id:s.local_vsr.cancel(record.local_vsr_job_id)
   s.relay.cancel_task(lease);s.state.update(lease.lease_id,status='cancelled');s._event('INFO','task_cancelled',lease)
  except LeaseLostError:s._lose(s.state.get(lease.lease_id),'lease lost while cancelling');return
  except RelayTransientError as exc:s._event('WARNING','relay_connection_lost',lease,error_code='RELAY_TRANSIENT',error=redact_text(exc));raise
  s._cleanup(lease)
 def _fail(s,lease,exc):
  message=redact_text(exc)[:2000]
  try:s.relay.fail_task(lease,'WORKER_EXECUTION_FAILED',message);s.state.update(lease.lease_id,status='failed');s._event('ERROR','task_failed',lease,error_code='WORKER_EXECUTION_FAILED',error=message)
  except LeaseLostError:s._lose(s.state.get(lease.lease_id),'lease lost while failing');return
  except RelayTransientError as relay_exc:s._diagnostic(lease.lease_id,message);s._event('WARNING','relay_connection_lost',lease,error_code='RELAY_TRANSIENT',error=redact_text(relay_exc));raise
  s._diagnostic(lease.lease_id,message);s._cleanup(lease)
 def _fail_ambiguous(s,lease):
  try:s.relay.fail_task(lease,'RECOVERY_AMBIGUOUS_LOCAL_JOB','local VSR job state is ambiguous after restart');s.state.update(lease.lease_id,status='failed')
  except LeaseLostError:s._lose(s.state.get(lease.lease_id),'lease lost during recovery');return
  except RelayTransientError as exc:s._diagnostic(lease.lease_id,'RECOVERY_AMBIGUOUS_LOCAL_JOB');s._event('WARNING','relay_connection_lost',lease,error_code='RELAY_TRANSIENT',error=redact_text(exc));raise
  s._diagnostic(lease.lease_id,'RECOVERY_AMBIGUOUS_LOCAL_JOB');s._cleanup(lease)
 def _lose(s,record,reason):
  if record:s.state.update(record.lease_id,status='lease_lost');s._event('WARNING','lease_lost',record,error_code='LEASE_LOST',error=reason);s._diagnostic(record.lease_id,reason);s._cleanup_record(record)
 def _cleanup(s,lease):
  record=s.state.get(lease.lease_id)
  if record:s._cleanup_record(record)
 def _cleanup_record(s,record):
  for asset_id in (record.local_input_asset_id,record.local_output_asset_id):
   if asset_id:
    try:s.local_vsr.delete_asset(asset_id)
    except Exception:pass
  shutil.rmtree(s.settings.runtime.state_dir/'tasks'/record.job_id/record.lease_id,ignore_errors=True);s._event('INFO','local_media_cleaned',record)
 def _diagnostic(s,lease_id,message):
  directory=s.settings.runtime.state_dir/'diagnostics';directory.mkdir(parents=True,exist_ok=True);(directory/f'{lease_id}.log').write_text(redact_text(message)+'\n',encoding='utf-8')
 def _dir(s,lease):return s.settings.runtime.state_dir/'tasks'/lease.job_id/lease.lease_id
 def _ensure_capacity(s):
  s.settings.runtime.state_dir.mkdir(parents=True,exist_ok=True)
  if shutil.disk_usage(s.settings.runtime.state_dir).free<s.settings.runtime.min_free_bytes:raise RuntimeError('local worker disk is below the configured safety waterline')
 def _wait_to_reconnect(s,attempt,exc,error_code):
  runtime=s.settings.runtime;initial=getattr(runtime,'relay_reconnect_initial_seconds',1);maximum=getattr(runtime,'relay_reconnect_max_seconds',30);delay=min(maximum,initial*(2**(attempt-1)));s._event('WARNING','reconnect_wait',error_code=error_code,reconnect_attempt=attempt,delay_seconds=delay,error=redact_text(exc));s.sleep(delay)
 def _event(s,level,event,subject=None,**fields):
  record=subject if hasattr(subject,'trace_id') else (s.state.get(subject.lease_id) if subject else None)
  if subject and hasattr(subject,'lease_id'):
   fields={"trace_id":subject.trace_id,"client_request_id":subject.client_request_id,"job_id":subject.job_id,"lease_id":subject.lease_id,"attempt":subject.attempt,**fields}
  elif record:
   fields={"trace_id":record.trace_id,"client_request_id":record.client_request_id,"job_id":record.job_id,"lease_id":record.lease_id,"attempt":record.attempt,"local_vsr_job_id":record.local_vsr_job_id,**fields}
  s.audit.emit(level,event,**fields)

class _LeaseControl:
 def __init__(s,runtime,lease,cancelled):s.runtime=runtime;s.lease=lease;s.cancelled=set(cancelled);s.heartbeat_at=s.renew_at=0.0
 def check(s,force_renew=False):
  now=time.monotonic()
  if now-s.heartbeat_at>=s.runtime.settings.runtime.heartbeat_seconds:
   heartbeat=s.runtime.relay.heartbeat(s.runtime._capabilities());s.heartbeat_at=now;s.cancelled.update(heartbeat.cancel_lease_ids);s.runtime._event('INFO','heartbeat',s.lease)
  if s.lease.lease_id in s.cancelled:raise _Cancelled()
  if force_renew or now-s.renew_at>=s.runtime.settings.runtime.lease_renew_seconds:
   _,cancel=s.runtime.relay.renew_lease(s.lease);s.renew_at=now;s.runtime._event('INFO','lease_renewed',s.lease)
   if cancel:raise _Cancelled()
