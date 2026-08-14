from __future__ import annotations
import hashlib, os
from pathlib import Path
from typing import Callable
from .state import WorkerState
def sha256_file(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()
def download_with_resume(dst:Path,size:int,sha:str,open_response:Callable[[int],object],progress=None)->Path:
 dst.parent.mkdir(parents=True,exist_ok=True); part=dst.with_suffix(dst.suffix+'.part'); n=part.stat().st_size if part.exists() else 0
 if n>size:part.unlink();n=0
 r=open_response(n)
 try:
  if n and r.status!=206:r.close();part.unlink(missing_ok=True);n=0;r=open_response(0)
  if r.status not in (200,206):raise RuntimeError('download failed')
  with part.open('ab' if n else 'wb') as f:
   while b:=r.read(1024*1024): f.write(b);n+=len(b); progress and progress(n,size)
 finally:r.close()
 if n!=size or sha256_file(part).lower()!=sha.lower():raise RuntimeError('download integrity check failed')
 os.replace(part,dst);return dst
def upload_output_with_resume(relay,lease,state:WorkerState,path:Path,progress=None)->str:
 size=path.stat().st_size; sha=sha256_file(path); s=relay.create_output_upload(lease,size,sha); state.update(lease.lease_id,status='uploading_output',upload_id=s.upload_id,upload_part_size=s.part_size_bytes); done=relay.get_upload_parts(s.upload_id); sent=0
 with path.open('rb') as f:
  number=0
  while b:=f.read(s.part_size_bytes):
   start=sent;sent+=len(b); digest=hashlib.sha256(b).hexdigest()
   if done.get(number)!=digest: relay.upload_part(s.upload_id,number,f'bytes {start}-{sent-1}/{size}',digest,b)
   state.save_upload_part(lease.lease_id,number,digest);progress and progress(sent,size);number+=1
 relay.complete_upload(s.upload_id,sha);return sha
