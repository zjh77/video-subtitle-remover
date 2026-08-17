from __future__ import annotations
import hashlib, http.client, os, socket, ssl
from pathlib import Path
from typing import Callable
from .state import WorkerState
from .relay_client import RelayProtocolError, RelayTransientError
def sha256_file(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()
def download_with_resume(dst:Path,size:int,sha:str,open_response:Callable[[int],object],progress=None,stats=None)->Path:
 dst.parent.mkdir(parents=True,exist_ok=True); part=dst.with_suffix(dst.suffix+'.part'); n=part.stat().st_size if part.exists() else 0
 if n>size:part.unlink();n=0
 initial=n
 if n==size:
  if sha256_file(part).lower()==sha.lower():_stats(stats,size,0,True);os.replace(part,dst);return dst
  part.unlink();n=0
 r=open_response(n)
 try:
  if n and r.status!=206:r.close();part.unlink(missing_ok=True);n=0;r=open_response(0)
  _validate_download_response(r,n,size)
  with part.open('ab' if n else 'wb') as f:
   try:
    while b:=r.read(1024*1024):
     if n+len(b)>size:raise RelayProtocolError('artifact download exceeded expected size')
     f.write(b);n+=len(b); progress and progress(n,size)
   except (OSError,http.client.HTTPException,ssl.SSLError,TimeoutError,socket.timeout) as exc:_stats(stats,size,max(0,n-initial),initial>0);raise RelayTransientError('artifact download stream interrupted') from exc
 finally:r.close()
 _stats(stats,size,max(0,n-initial),initial>0)
 if n!=size:raise RelayTransientError('artifact download ended before expected size')
 if sha256_file(part).lower()!=sha.lower():part.unlink(missing_ok=True);raise RuntimeError('artifact download SHA-256 verification failed')
 os.replace(part,dst);return dst

def _stats(stats,size,transferred,resumed):
 if stats is not None:stats.update(size_bytes=size,transferred_bytes=transferred,resumed=resumed)

def _header(response,name):
 headers=getattr(response,'headers',None)
 if headers is not None:return headers.get(name)
 if hasattr(response,'header'):return response.header(name)
 return None

def _validate_download_response(response,start,size):
 expected_status=206 if start else 200
 if response.status!=expected_status:raise RelayProtocolError('artifact download returned unexpected range status')
 expected_length=size-start;length=_header(response,'Content-Length')
 if length is not None and length!=str(expected_length):raise RelayProtocolError('artifact download Content-Length does not match lease')
 if start:
  expected_range=f'bytes {start}-{size-1}/{size}'
  if _header(response,'Content-Range')!=expected_range:raise RelayProtocolError('artifact download Content-Range does not match resume offset')
def upload_output_with_resume(relay,lease,state:WorkerState,path:Path,progress=None,stats=None)->str:
 size=path.stat().st_size; sha=sha256_file(path); s=relay.create_output_upload(lease,size,sha); state.update(lease.lease_id,status='uploading_output',upload_id=s.upload_id,upload_part_size=s.part_size_bytes); done=relay.get_upload_parts(s.upload_id); sent=transferred=0
 with path.open('rb') as f:
  number=0
  while b:=f.read(s.part_size_bytes):
   start=sent;sent+=len(b); digest=hashlib.sha256(b).hexdigest()
   if done.get(number)!=digest: relay.upload_part(s.upload_id,number,f'bytes {start}-{sent-1}/{size}',digest,b);transferred+=len(b)
   state.save_upload_part(lease.lease_id,number,digest);progress and progress(sent,size);number+=1
 relay.complete_upload(s.upload_id,sha);_stats(stats,size,transferred,bool(done) or bool(getattr(s,'reused',False)));return sha
