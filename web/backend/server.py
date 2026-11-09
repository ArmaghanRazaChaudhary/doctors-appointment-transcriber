"""Same-origin localhost server. Bind loopback; never serve source files or keys."""
import argparse
import hashlib
import json
import mimetypes
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from pydantic import ValidationError
import store
import pipeline
import streaming

TOKEN = secrets.token_urlsafe(32)
PORT = 8766
STATIC = store.ROOT / 'dist' / 'client'
# Windows file associations can label .js as text/plain; modules require JavaScript MIME.
STATIC_TYPES={'.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.html':'text/html; charset=utf-8','.json':'application/json; charset=utf-8'}
ACTIVE = {'queued','transcribing','structuring','recording','finishing','capture_interrupted','transcription_incomplete'}

def check_note(result):
    note = result['note']
    original = {k:v for k,v in note.items() if k != 'ai_diagnosis_paragraph'}
    pipeline.ClinicalNote.model_validate(original)
    if 'ai_diagnosis_paragraph' in note and not isinstance(note['ai_diagnosis_paragraph'], str):
        raise ValueError('Invalid AI paragraph')
    return result

def patient_fields(data):
    patient = data.get('patient', {})
    for k in ['name','patient_id']:
        if not isinstance(patient.get(k),str) or not patient[k].strip() or len(patient[k])>120:
            raise ValueError('Patient name and ID are required')
    doctor = data.get('doctor','')
    if not isinstance(doctor,str) or not doctor.strip() or len(doctor)>120:
        raise ValueError('Doctor name is required')
    return {'name':patient['name'].strip(),'patient_id':patient['patient_id'].strip()}, doctor.strip()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def headers_common(self):
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Permissions-Policy','microphone=(self), camera=(), geolocation=()')
        self.send_header('X-Frame-Options','DENY')
    def reply(self, code, data):
        payload=json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.send_response(code);self.headers_common()
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def protect(self, token=True):
        hosts={f'localhost:{PORT}',f'127.0.0.1:{PORT}'}
        if self.headers.get('Host') not in hosts:
            raise PermissionError('Invalid host')
        origin=self.headers.get('Origin')
        if origin and origin not in {f'http://{h}' for h in hosts}:
            raise PermissionError('Invalid origin')
        if self.headers.get('Sec-Fetch-Site')=='cross-site':
            raise PermissionError('Cross-site request blocked')
        if token and not secrets.compare_digest(self.headers.get('X-Suno-Token',''),TOKEN):
            raise PermissionError('Reload the app to reconnect')
    def body(self, limit=1_000_000):
        size=int(self.headers.get('Content-Length','0'))
        if size<=0 or size>limit:
            raise ValueError('Request is empty or too large')
        data=self.rfile.read(size)
        if len(data)!=size:raise ValueError('Upload incomplete')
        return data
    def do_GET(self):
        try:
            url=urlparse(self.path)
            self.protect(token=url.path.startswith('/api/') and url.path!='/api/session')
            if url.path=='/api/session':
                return self.reply(200,dict(token=TOKEN,key_ready=bool(pipeline.get_key()),models=pipeline.SETTINGS))
            if url.path=='/api/consultations':
                workspace=parse_qs(url.query).get('workspace',['clinic'])[0]
                return self.reply(200,store.list_all(workspace))
            if url.path.startswith('/api/consultations/'):
                parts=url.path.strip('/').split('/')
                c=store.load(parts[2])
                if len(parts)==4 and parts[3]=='evidence':
                    path=store.directory(c['id'])/'transcript.txt'
                    original=store.directory(c['id'])/'original_note.json'
                    transcript=path.read_text(encoding='utf-8') if path.exists() else ''
                    rendered=store.directory(c['id'])/'transcript_urdu.json'
                    view=None
                    if rendered.exists():
                        saved=json.loads(rendered.read_text(encoding='utf-8'))
                        if saved.get('source_sha256')==hashlib.sha256(transcript.encode('utf-8')).hexdigest():view=saved['text']
                    return self.reply(200,dict(transcript=view or transcript,original_transcript=transcript if view else None,display_language='ur' if view else None,original=json.loads(original.read_text(encoding='utf-8')) if original.exists() else None))
                if len(parts)==4 and parts[3] in ('raw-audio','chunk-audio'):
                    path=store.directory(c['id'])/'raw.wav'
                    if parts[3]=='chunk-audio':
                        seq=int(parse_qs(url.query).get('seq',['-1'])[0])
                        if seq<0:raise ValueError('Invalid chunk')
                        path=store.directory(c['id'])/'chunks'/f'{seq:06d}.wav'
                    with store.LOCK:content=path.read_bytes()
                    self.send_response(200);self.headers_common()
                    self.send_header('Content-Type','audio/wav');self.send_header('Content-Length',str(len(content)))
                    self.end_headers();self.wfile.write(content);return
                if len(parts)!=3:raise FileNotFoundError()
                return self.reply(200,streaming.status(c['id']) if c.get('live') else c)
            if url.path.startswith('/api/'):raise FileNotFoundError()
            relative=unquote(url.path).lstrip('/') or 'index.html'
            path=(STATIC/relative).resolve()
            if not path.is_relative_to(STATIC.resolve()) or not path.is_file():raise FileNotFoundError()
            if path.suffix not in {'.html','.js','.css','.svg','.png','.ico','.woff','.woff2','.txt','.json'}:raise FileNotFoundError()
            content=path.read_bytes();self.send_response(200);self.headers_common()
            self.send_header('Content-Type',STATIC_TYPES.get(path.suffix) or mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        except PermissionError as e:self.reply(403,{'error':str(e)})
        except (FileNotFoundError,KeyError):self.reply(404,{'error':'Consultation or file not found'})
        except (ValueError,TypeError):self.reply(400,{'error':'Invalid request'})
    def do_POST(self):
        try:
            self.protect()
            url=urlparse(self.path);parts=url.path.strip('/').split('/')
            if url.path=='/api/consultations':
                data=json.loads(self.body());patient,doctor=patient_fields(data)
                if data.get('workspace') not in ['clinic','hospital']:raise ValueError('Invalid workspace')
                return self.reply(201,store.create(patient,data['workspace'],doctor))
            if len(parts)!=4 or parts[:2]!=['api','consultations']:raise FileNotFoundError()
            cid,action=parts[2:]
            if action=='live-part':
                seq=int(parse_qs(url.query).get('seq',['-1'])[0])
                streaming.accept(cid,seq,self.body())
                return self.reply(200,{'accepted':seq})
            if action in ('live-start','live-end'):
                data=json.loads(self.body())
                if action=='live-start':
                    if self.headers.get('X-Recording-Consent')!='yes':raise ValueError('Recording consent required')
                    result=streaming.start(cid,data.get('sample_rate'))
                else:result=streaming.end(cid,data.get('expected_parts'),data.get('total_samples'),data.get('interrupted',False))
                return self.reply(200,result)
            if action=='chunk-text':return self.reply(410,{'error':'Audio chunk correction is not part of the doctor workflow.'})
            if action=='retry':return self.reply(409,{'error':'Paid retries are disabled. Your saved source is retained.'})
            if action=='audio':
                if not pipeline.get_key():return self.reply(503,{'error':'API key missing. Set openai_trial/api-key.txt.'})
                if self.headers.get('X-Recording-Consent')!='yes':raise ValueError('Recording and processing consent required')
                filename=parse_qs(url.query).get('filename',['recording.webm'])[0]
                ext=Path(filename).suffix.lower()
                if ext not in {'.wav','.mp3','.mp4','.mpeg','.mpga','.m4a','.webm','.ogg','.flac'}:raise ValueError('Unsupported audio format')
                audio=self.body(25_000_000);digest=hashlib.sha256(audio).hexdigest()
                with store.LOCK:
                    c=store.load(cid)
                    if c.get('audio_sha256')==digest:return self.reply(200,c)
                    if c['state']!='ready':return self.reply(409,{'error':'Create a new consultation for different audio'})
                    audio_path=store.directory(cid)/('audio'+ext);audio_path.write_bytes(audio)
                    c.update(state='queued',audio_file=audio_path.name,audio_sha256=digest,error=None,consent_at=store.now())
                    store.save(c)
                pipeline.submit(cid)
                return self.reply(202,c)
            data=json.loads(self.body())
            with store.LOCK:
                c=store.load(cid)
                if action not in ['draft','confirm']:raise FileNotFoundError()
                if c['state'] in ACTIVE or c['result'] is None:raise ValueError('Wait for a clinical draft')
                if data.get('revision')!=c['revision']:return self.reply(409,{'error':'This consultation changed. Reload before saving.'})
                if action=='confirm' and data.get('reviewed') is not True:raise ValueError('Review confirmation required')
                patient,doctor=patient_fields(data)
                result=check_note(data['result'])
                original=c['result']['note']
                # AI assistance is immutable. Clinician edits remain in the clinical fields.
                result['note']['diagnostic_assistance']=original['diagnostic_assistance']
                if 'ai_diagnosis_paragraph' in original:result['note']['ai_diagnosis_paragraph']=original['ai_diagnosis_paragraph']
                result['status']='reviewed' if action=='confirm' else 'pending_doctor_review'
                c.update(result=result,patient=patient,doctor=doctor,revision=c['revision']+1,updated_at=store.now(),state='reviewed' if action=='confirm' else 'review_required',reviewed_at=store.now() if action=='confirm' else None)
                store.write_json(store.directory(cid)/'revisions'/f'{c["revision"]:04d}.json',c)
                store.save(c)
                return self.reply(200,streaming.status(cid) if c.get('live') else c)
        except PermissionError as e:self.reply(403,{'error':str(e)})
        except (FileNotFoundError,KeyError):self.reply(404,{'error':'Consultation or action not found'})
        except (ValueError,TypeError,ValidationError) as e:self.reply(400,{'error':'Invalid clinical data or request. '+str(e).split('\n')[0][:180]})
        except OSError:self.reply(500,{'error':'Could not save to disk. Check available space and permissions.'})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8766);args=parser.parse_args();PORT=args.port
    store.DATA.mkdir(parents=True,exist_ok=True);store.import_saved();store.recover();streaming.recover()
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    print(f'Suno app: http://localhost:{PORT}',flush=True)
    print('API key configured: '+str(bool(pipeline.get_key())),flush=True)
    server.serve_forever()
