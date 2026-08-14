import hashlib,unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from vsr_worker.models import ClaimedLease
from vsr_worker.state import WorkerState
from vsr_worker.transfer import download_with_resume,upload_output_with_resume
def lease():return ClaimedLease.from_response({'lease_id':'att_1','job_id':'vcj_1','attempt':1,'lease_expires_at':'x','operation':{'type':'subtitle_cleanup','options':{'subtitle_areas':[{'ymin':0,'ymax':1,'xmin':0,'xmax':1}],'inpaint_mode':'opencv'}},'input':{'artifact_id':'art_1','size_bytes':3,'sha256':hashlib.sha256(b'abc').hexdigest(),'download_url':'/api/v1/artifacts/art_1/download?token=x'}})
class R:
 def __init__(s,n):s.status=206 if n else 200;s.x=BytesIO(b'abc'[n:])
 def read(s,n=-1):return s.x.read(n)
 def close(s):pass
class Relay:
 def __init__(s):s.parts={};s.completed=False
 def create_output_upload(s,*x):return type('S',(),{'upload_id':'upl_1','part_size_bytes':2})()
 def get_upload_parts(s,u):return s.parts
 def upload_part(s,u,n,r,h,b):s.parts[n]=h;return h
 def complete_upload(s,u,h):s.completed=True
class T(unittest.TestCase):
 def test_claim_download_parts_complete(self):
  with TemporaryDirectory() as d:
   root=Path(d);st=WorkerState(root);st.initialize();l=lease();st.save_claim(l);src=download_with_resume(root/'in.mp4',3,l.input_artifact.sha256,lambda n:R(n));out=root/'out.mp4';out.write_bytes(b'abcdef');r=Relay();self.assertEqual(upload_output_with_resume(r,l,st,out),hashlib.sha256(b'abcdef').hexdigest());self.assertTrue(r.completed)
 def test_operation_rejects_missing_options(self):
  with self.assertRaises(ValueError):ClaimedLease.from_response({'lease_id':'a','job_id':'j','attempt':1,'lease_expires_at':'x','operation':{'type':'subtitle_cleanup'},'input':{'artifact_id':'i','size_bytes':1,'sha256':'a'*64,'download_url':'/'}})
if __name__=='__main__':unittest.main()
