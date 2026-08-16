"""Private-CA HTTPS adapter for video-task-server lease endpoints."""
from __future__ import annotations
import json, socket, ssl
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from .config import RelaySettings
from .models import ClaimedLease, UploadSession
class RelayError(RuntimeError): pass
class RelayProtocolError(RelayError): pass
class RelayTransientError(RelayError): pass
class LeaseLostError(RelayError): pass
class _NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args): return None
@dataclass(frozen=True)
class HeartbeatResult: cancel_lease_ids:frozenset[str]
class RelayClient:
 API_PREFIX="/api/v1"
 def __init__(self,s:RelaySettings):
  self.settings=s; self.origin=s.base_url.rstrip("/"); self.parts=urlsplit(self.origin); c=ssl.create_default_context(cafile=str(s.ca_file)); c.minimum_version=ssl.TLSVersion.TLSv1_2; self.opener=build_opener(_NoRedirect(),HTTPSHandler(context=c))
 def register(self,display_name,capabilities): return self._json("POST",f"/workers/{self._worker()}/register",{"display_name":display_name,"capabilities":capabilities})
 def heartbeat(self,capabilities=None):
  v=self._json("POST",f"/workers/{self._worker()}/heartbeat",{} if capabilities is None else {"capabilities":capabilities}); x=v.get("cancel_requested",[])
  if not isinstance(x,list): raise RelayProtocolError("heartbeat cancellation list is invalid")
  return HeartbeatResult(frozenset(str(i["attempt_id"]) for i in x if isinstance(i,dict) and i.get("attempt_id")))
 def claim(self,wait):
  status,v=self._json_status("POST",f"/workers/{self._worker()}/leases:claim",{"max_jobs":1,"wait_seconds":wait},wait+10,True); return None if status==204 else ClaimedLease.from_response(v)
 def recover_lease(self,lease_id):
  try:return ClaimedLease.from_response(self._json("GET",f"/worker-leases/{quote(lease_id,safe='')}"))
  except RelayError as exc:
   if "HTTP 404" in str(exc):raise LeaseLostError("relay recovery lease is unavailable") from exc
   raise
 def renew_lease(self,c):
  v=self._json("POST",self._lease(c)+"/renew"); return str(v["lease_expires_at"]),bool(v["cancel_requested"])
 def report_progress(self,c,p,message): self._json("POST",self._lease(c)+"/progress",{"progress_percent":max(0,min(100,round(p or 0))),"message":message[:500]})
 def report_log(self,c,seq,level,message): self._json("POST",self._lease(c)+"/logs",{"seq":seq,"level":level.upper()[:16],"message":message[:4000]})
 def create_output_upload(self,c,size,sha):
  v=self._json("POST",self._lease(c)+"/output-upload",{"size_bytes":size,"expected_sha256":sha}); return UploadSession(str(v["upload_id"]),str(v["artifact_id"]),int(v["part_size_bytes"]),bool(v["reused"]))
 def get_upload_parts(self,u):
  v=self._json("GET",f"/uploads/{quote(u,safe='')}"); return {int(i["part_number"]):str(i["sha256"]) for i in v["parts"]}
 def upload_part(self,u,n,r,sha,body): return str(self._json("PUT",f"/uploads/{quote(u,safe='')}/parts/{n}",body,{"Content-Range":r,"X-Part-SHA256":sha,"Content-Type":"application/octet-stream"})["sha256"])
 def complete_upload(self,u,sha): self._json("POST",f"/uploads/{quote(u,safe='')}/complete",{"sha256":sha})
 def complete_task(self,c,sha): self._json("POST",self._lease(c)+"/complete",{"output_sha256":sha})
 def fail_task(self,c,code,msg): self._json("POST",self._lease(c)+"/fail",{"error_code":code,"error_message":msg[:2000]})
 def cancel_task(self,c): self._json("POST",self._lease(c)+"/cancelled")
 def open_input(self,url,start):
  absolute=urljoin(self.origin+"/",url); p=urlsplit(absolute)
  if p.scheme!="https" or p.netloc!=self.parts.netloc: raise RelayProtocolError("artifact URL is outside relay origin")
  try:return self.opener.open(Request(absolute,headers={"Range":f"bytes={start}-"} if start else {},method="GET"),timeout=60)
  except HTTPError as e: raise self._http_error(e,"artifact download") from e
  except (URLError,ssl.SSLError,TimeoutError,socket.timeout) as e: raise self._connection_error(e,"artifact download") from e
 def _lease(self,c): return f"/worker-leases/{quote(c.lease_id,safe='')}"
 def _worker(self): return quote(self.settings.worker_id,safe="")
 def _json(self,m,p,data=None,h=None): return self._json_status(m,p,data,30,False,h)[1]
 def _json_status(self,m,p,data,timeout,empty,h=None):
  headers={"Accept":"application/json","Authorization":f"Bearer {self.settings.worker_token}","User-Agent":"vsr-worker/0.1"}; headers.update(h or {})
  body=data if isinstance(data,bytes) else (json.dumps(data,separators=(",",":")).encode() if data is not None else None)
  if data is not None and not isinstance(data,bytes): headers.setdefault("Content-Type","application/json")
  try:
   with self.opener.open(Request(urljoin(self.origin+self.API_PREFIX+"/",p.lstrip("/")),data=body,headers=headers,method=m),timeout=timeout) as r: status,raw=r.status,r.read()
  except HTTPError as e:
   if empty and e.code==204:return 204,{}
   if e.code in {401,403,409}:raise LeaseLostError(f"relay rejected request with HTTP {e.code}") from e
   raise self._http_error(e,"relay request") from e
  except (URLError,ssl.SSLError,TimeoutError,socket.timeout) as e: raise self._connection_error(e,"relay HTTPS") from e
  if empty and status==204:return 204,{}
  try:v=json.loads(raw)
  except Exception as e:raise RelayProtocolError("relay response was not valid JSON") from e
  if not isinstance(v,dict):raise RelayProtocolError("relay response JSON must be object")
  return status,v
 def _http_error(self,error,context):
  if error.code in {408,429} or error.code>=500:return RelayTransientError(f"{context} temporarily unavailable HTTP {error.code}")
  return RelayProtocolError(f"{context} returned unexpected HTTP {error.code}")
 def _connection_error(self,error,context):
  reason=getattr(error,"reason",error)
  if isinstance(reason,ssl.SSLCertVerificationError):return RelayProtocolError(f"{context} TLS validation failed")
  if isinstance(reason,ssl.SSLError):return RelayProtocolError(f"{context} TLS connection failed")
  return RelayTransientError(f"{context} connection failed")
