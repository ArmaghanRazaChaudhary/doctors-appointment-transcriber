"""Offline HTTP integration tests. No paid API requests; isolated temporary records."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request,urlopen
from urllib.error import HTTPError

TEMP=tempfile.TemporaryDirectory(prefix='suno-http-test-')
os.environ['SUNO_DATA_DIR']=TEMP.name
import server
import pipeline
import store
from http.server import ThreadingHTTPServer

FIXTURE=json.loads((store.ROOT/'data'/'saved-runs.json').read_text(encoding='utf-8'))[0]['result']
COUNTS={'audio':0,'clinical':0}
FAIL=False
class FakeClient:
    def __init__(self,**kw):self.responses=self
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def parse(self,**kw):
        COUNTS['clinical']+=1
        if FAIL:raise TimeoutError('offline simulated failure')
        self_note=copy.deepcopy(FIXTURE['note']);self_note['ai_diagnosis_paragraph']='Provisional test paragraph. Review clinical findings.'
        return SimpleNamespace(status='completed',output_parsed=pipeline.WebNote.model_validate(self_note),model='offline-test',usage=None,reasoning=None,id='test-response',_request_id='test-request')
def fake_transcribe(client,path,log):
    COUNTS['audio']+=1
    assert path.exists() and path.read_bytes()==b'RIFF-test-audio'
    return SimpleNamespace(text='Test transcript',model_dump=lambda **kw:{'text':'Test transcript'})
pipeline.OpenAI=FakeClient
pipeline.transcribe=fake_transcribe
pipeline.get_key=lambda:'offline-test-key'
HTTP=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
server.PORT=HTTP.server_port
threading.Thread(target=HTTP.serve_forever,daemon=True).start()
BASE='http://127.0.0.1:'+str(server.PORT)
def request(path,body=None,headers=None,method=None):
    h={'X-Suno-Token':server.TOKEN,'Origin':BASE}
    if headers:h.update(headers)
    if isinstance(body,dict):body=json.dumps(body).encode();h['Content-Type']='application/json'
    req=Request(BASE+path,data=body,headers=h,method=method)
    try:
        with urlopen(req,timeout=5) as r:return r.status,json.load(r)
    except HTTPError as e:return e.code,json.load(e)
def create(workspace='clinic'):
    status,c=request('/api/consultations',{'workspace':workspace,'patient':{'name':'Test Patient','patient_id':'TEST-HTTP'},'doctor':'Test Doctor'})
    assert status==201
    return c

class Flow(unittest.TestCase):
    def test_end_to_end_upload_review_and_evidence(self):
        c=create();cid=c['id'];url='/api/consultations/'+cid
        code,_=request(url+'/audio?filename=test.wav',b'RIFF-test-audio',{'X-Recording-Consent':'yes'})
        self.assertEqual(code,202)
        for _ in range(100):
            _,c=request(url)
            if c['state'] not in server.ACTIVE:break
            time.sleep(.05)
        self.assertEqual(c['state'],'review_required',c.get('error'))
        request(url+'/audio?filename=test.wav',b'RIFF-test-audio',{'X-Recording-Consent':'yes'})
        self.assertEqual(COUNTS,{'audio':1,'clinical':1})
        self.assertEqual(c['patient']['patient_id'],'TEST-HTTP')
        original=copy.deepcopy(c['result']);edited=copy.deepcopy(original)
        edited['note']['clinician_assessment'][0]['description']='Doctor corrected assessment'
        payload={'revision':c['revision'],'result':edited,'patient':c['patient'],'doctor':c['doctor'],'reviewed':True}
        code,reviewed=request(url+'/confirm',payload)
        self.assertEqual(code,200);self.assertEqual(reviewed['state'],'reviewed')
        self.assertEqual(reviewed['result']['note']['clinician_assessment'][0]['description'],'Doctor corrected assessment')
        _,evidence=request(url+'/evidence')
        self.assertEqual(evidence['original'],original)
        self.assertEqual(request(url+'/confirm',payload)[0],409)
        self.assertTrue((store.directory(cid)/'revisions'/'0001.json').exists())
        self.assertEqual(store.load(cid)['state'],'reviewed')
    def test_boundary_rejections_and_workspace(self):
        self.assertEqual(request('/api/consultations',headers={'X-Suno-Token':''})[0],403)
        self.assertEqual(request('/api/session',headers={'Origin':'https://other.example'})[0],403)
        self.assertEqual(request('/api/session',headers={'Host':'attacker.example'})[0],403)
        c=create('hospital');path='/api/consultations/'+c['id']+'/audio?filename=test.wav'
        self.assertEqual(request(path,b'RIFF-test-audio')[0],400)
        self.assertEqual(request(path.replace('.wav','.exe'),b'RIFF-test-audio',{'X-Recording-Consent':'yes'})[0],400)
        _,clinic=request('/api/consultations?workspace=clinic')
        self.assertNotIn(c['id'],[x['id'] for x in clinic])
        self.assertEqual(request('/api/consultations/'+c['id']+'/confirm',{'revision':0,'result':FIXTURE,'reviewed':True})[0],400)
    def test_interrupted_job_recovery(self):
        c=create();c['state']='transcribing';store.save(c);store.recover()
        self.assertEqual(store.load(c['id'])['state'],'failed')
    def test_failed_structuring_is_not_retried(self):
        global FAIL
        before=COUNTS['audio'];FAIL=True
        c=create();url='/api/consultations/'+c['id']
        request(url+'/audio?filename=test.wav',b'RIFF-test-audio',{'X-Recording-Consent':'yes'})
        for _ in range(100):
            _,c=request(url)
            if c['state'] not in server.ACTIVE:break
            time.sleep(.05)
        self.assertEqual(c['state'],'failed')
        self.assertTrue((store.directory(c['id'])/'transcript.txt').exists())
        FAIL=False
        clinical_before=COUNTS['clinical']
        self.assertEqual(request(url+'/retry',{'confirm_cost':True})[0],409)
        self.assertEqual(COUNTS['clinical'],clinical_before)
        self.assertEqual(COUNTS['audio'],before+1)

    def test_invalid_clinical_schema(self):
        broken=copy.deepcopy(FIXTURE);broken['note']['medications'][0]['dose']={'invalid':'object'}
        with self.assertRaises(Exception):server.check_note(broken)

if __name__=='__main__':
    try:
        result=unittest.main(exit=False).result
        if not result.wasSuccessful():raise SystemExit(1)
    finally:HTTP.shutdown();pipeline.POOL.shutdown(wait=True)
