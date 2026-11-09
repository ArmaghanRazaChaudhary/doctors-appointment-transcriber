"""Transactional consultation state; original source artifacts remain separate."""
import json
from contextlib import contextmanager
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
DATA=Path(os.getenv('SUNO_DATA_DIR',str(ROOT/'local_data')))
LOCK=threading.RLock()
def now():return datetime.now(timezone.utc).isoformat()
def directory(cid):
    if len(cid)!=32 or any(c not in '0123456789abcdef' for c in cid):raise ValueError('Invalid consultation ID')
    path=DATA/cid;path.mkdir(parents=True,exist_ok=True);return path

@contextmanager
def connection():
    DATA.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA/'consultations.db',timeout=15)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS consultations (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, created_at TEXT NOT NULL, document TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS consultations_workspace_time ON consultations(workspace,created_at)')
    db.execute('CREATE TABLE IF NOT EXISTS imports (source TEXT PRIMARY KEY)')
    try:
        with db:
            yield db
    finally:
        db.close()

def write_json(path,value):
    # Immutable artifacts use new filenames. Transactional state lives in SQLite.
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2)
        f.flush();os.fsync(f.fileno())

def load(cid):
    directory(cid)
    with LOCK,connection() as db:
        row=db.execute('SELECT document FROM consultations WHERE id=?',(cid,)).fetchone()
    if not row:raise FileNotFoundError()
    return json.loads(row[0])

def save(c):
    with LOCK,connection() as db:
        db.execute('INSERT INTO consultations VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET workspace=excluded.workspace,created_at=excluded.created_at,document=excluded.document',(c['id'],c['workspace'],c['created_at'],json.dumps(c,ensure_ascii=False)))

def create(patient,workspace,doctor):
    c=dict(id=uuid4().hex,patient=patient,workspace=workspace,doctor=doctor,created_at=now(),updated_at=now(),state='ready',revision=0,result=None,error=None,reviewed_at=None,source='recording')
    save(c);return c

def list_all(workspace):
    with LOCK,connection() as db:
        rows=db.execute('SELECT document FROM consultations WHERE workspace=? ORDER BY created_at DESC',(workspace,)).fetchall()
    return [{k:c.get(k) for k in ['id','patient','created_at','state','source','revision']} for c in (json.loads(r[0]) for r in rows)]

def import_saved():
    source=ROOT.parent/'openai_trial'/'outputs'
    for path in sorted(source.glob('*/note.json'),reverse=True)[:3]:
        for workspace in ['clinic','hospital']:
            token=workspace+':'+path.parent.name
            with LOCK,connection() as db:
                if db.execute('SELECT 1 FROM imports WHERE source=?',(token,)).fetchone():continue
                result=json.loads(path.read_text(encoding='utf-8'))
                result['note'].setdefault('diagnostic_assistance',{'source':'ai_generated','review_status':'pending_doctor_review','suggestions':[],'insufficient_information_reason':'Not included in saved run'})
                c=dict(id=uuid4().hex,patient={'name':'Saved test run','patient_id':'TEST-'+path.parent.name[-8:]},doctor='Demo clinician',workspace=workspace,created_at=datetime.strptime(path.parent.name[:16],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).isoformat(),updated_at=now(),state='review_required',revision=0,result=result,error=None,reviewed_at=None,source=path.parent.name)
                write_json(directory(c['id'])/'original_note.json',json.loads(path.read_text(encoding='utf-8')))
                transcript=path.parent/'transcript.txt'
                if transcript.exists():(directory(c['id'])/'transcript.txt').write_text(transcript.read_text(encoding='utf-8'),encoding='utf-8')
                db.execute('INSERT INTO consultations VALUES (?,?,?,?)',(c['id'],workspace,c['created_at'],json.dumps(c,ensure_ascii=False)))
                db.execute('INSERT INTO imports VALUES (?)',(token,))

def recover():
    for workspace in ['clinic','hospital']:
        for summary in list_all(workspace):
            c=load(summary['id'])
            if c['state'] in ['queued','transcribing','structuring']:
                c.update(state='failed',error='Server restarted during processing. Saved evidence is retained; paid requests are not retried.');save(c)
