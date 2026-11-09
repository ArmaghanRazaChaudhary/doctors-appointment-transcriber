"""Durable, numbered raw transport and at-most-once paid chunk transcription."""
import hashlib
import json
import os
import re
import struct
import wave
from concurrent.futures import ThreadPoolExecutor
import store
import pipeline
from segmentation import PauseSegmenter
from realtime_transcription import RealtimeTranscriber,ContinuityError
from transcription import TRANSCRIPTION_PROMPT

POOL=ThreadPoolExecutor(max_workers=4)
SESSIONS={};RUNNING=set();TRANSCRIBERS={}

def init():
    with store.connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS stream_parts (cid TEXT, seq INTEGER, digest TEXT NOT NULL, pcm BLOB NOT NULL, PRIMARY KEY(cid,seq))')
        db.execute('CREATE TABLE IF NOT EXISTS speech_chunks (cid TEXT, seq INTEGER, document TEXT NOT NULL, PRIMARY KEY(cid,seq))')

def chunks(cid):
    with store.connection() as db:return [json.loads(r[0]) for r in db.execute('SELECT document FROM speech_chunks WHERE cid=? ORDER BY seq',(cid,))]

def save_chunk(cid,chunk):
    with store.connection() as db:db.execute('UPDATE speech_chunks SET document=? WHERE cid=? AND seq=?',(json.dumps(chunk,ensure_ascii=False),cid,chunk['seq']))

def wav_header(rate,size):
    return struct.pack('<4sI4s4sIHHIIHH4sI',b'RIFF',36+size,b'WAVE',b'fmt ',16,1,1,rate,rate*2,2,16,b'data',size)

class Session:
    def __init__(self,c):
        self.cid=c['id'];self.rate=c['live']['sample_rate'];self.next_part=0;self.total=0;self.next_chunk=0
        self.segmenter=PauseSegmenter(self.rate,self.emit)
        self.path=store.directory(self.cid)/'raw.wav'
        # SQLite is authoritative; rebuilding this single WAV is safe after interruption.
        with self.path.open('wb') as f:f.write(wav_header(self.rate,0))
        self.drain()
    def emit(self,start,end,pcm,overlap,forced):
        seq=self.next_chunk;self.next_chunk+=1
        with store.connection() as db:
            prior=db.execute('SELECT document FROM speech_chunks WHERE cid=? AND seq=?',(self.cid,seq)).fetchone()
        digest=hashlib.sha256(pcm).hexdigest()
        if prior:
            if json.loads(prior[0])['sha256']!=digest:raise ValueError('Chunk reconstruction mismatch; original recording preserved')
            return
        path=store.directory(self.cid)/'chunks'/f'{seq:06d}.wav';path.parent.mkdir(exist_ok=True)
        with wave.open(str(path),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(self.rate);f.writeframes(pcm)
        chunk=dict(seq=seq,start_sample=start,end_sample=end,overlap=overlap,forced_boundary=forced,sha256=digest,status='queued',text='',error=None)
        with store.connection() as db:db.execute('INSERT INTO speech_chunks VALUES (?,?,?)',(self.cid,seq,json.dumps(chunk)))
    def drain(self):
        while True:
            with store.connection() as db:row=db.execute('SELECT pcm FROM stream_parts WHERE cid=? AND seq=?',(self.cid,self.next_part)).fetchone()
            if row is None:break
            pcm=row[0]
            with self.path.open('r+b') as f:
                f.seek(44+self.total*2);f.write(pcm);self.total+=len(pcm)//2
                f.seek(0);f.write(wav_header(self.rate,self.total*2));f.flush();os.fsync(f.fileno())
            self.segmenter.feed(pcm);self.next_part+=1

def session(cid):
    if cid not in SESSIONS:SESSIONS[cid]=Session(store.load(cid))
    return SESSIONS[cid]

def start(cid,rate):
    init()
    if rate not in (8000,16000,32000,48000):raise ValueError('Unsupported microphone sample rate')
    with store.LOCK:
        c=store.load(cid)
        if c.get('live'):
            if c['live']['sample_rate']!=rate:raise ValueError('Sample rate mismatch')
            return status(cid)
        if c['state']!='ready':raise ValueError('Create a new consultation before recording')
        if not pipeline.get_key():raise ValueError('API key missing')
        c.update(state='recording',consent_at=store.now(),live={'sample_rate':rate,'pause_ms':500,'padding_ms':500,'raw_complete':False,'structured_once':False,'continuity':{'state':'new'}})
        store.save(c);session(cid)
    return status(cid)

def accept(cid,seq,pcm):
    if seq<0 or seq>200000 or not pcm or len(pcm)%2 or len(pcm)>1_000_000:raise ValueError('Invalid PCM block')
    with store.LOCK:
        c=store.load(cid)
        if not c.get('live'):raise ValueError('Start consultation first')
        digest=hashlib.sha256(pcm).hexdigest()
        with store.connection() as db:
            prior=db.execute('SELECT digest FROM stream_parts WHERE cid=? AND seq=?',(cid,seq)).fetchone()
            if prior:
                if prior[0]!=digest:raise ValueError('Same block number has different audio')
            else:
                if c['live']['raw_complete']:raise ValueError('Recording already complete')
                if c['state'] not in ('recording','capture_interrupted'):raise ValueError('Session is not accepting audio')
                last=db.execute('SELECT COALESCE(MAX(seq),-1) FROM stream_parts WHERE cid=?',(cid,)).fetchone()[0]
                if seq>last+120:raise ValueError('Too many missing audio blocks')
                db.execute('INSERT INTO stream_parts VALUES (?,?,?,?)',(cid,seq,digest,pcm))
        if not c['live']['raw_complete']:
            try:session(cid).drain()
            except Exception:
                SESSIONS.pop(cid,None)
                raise
    kick(cid)
    return {'accepted':seq}

def end(cid,expected,total,interrupted=False):
    if not isinstance(expected,int) or not isinstance(total,int) or expected<0 or total<0:raise ValueError('Invalid end manifest')
    with store.LOCK:
        c=store.load(cid)
        if not c.get('live'):raise ValueError('Not a live recording')
        if c['live']['raw_complete']:
            if c['live']['expected_parts']!=expected or c['live']['total_samples']!=total:raise ValueError('Conflicting end manifest')
            return status(cid)
        s=session(cid);s.drain()
        with store.connection() as db:count=db.execute('SELECT COUNT(*) FROM stream_parts WHERE cid=?',(cid,)).fetchone()[0]
        if count!=expected or s.next_part!=expected or s.total!=total:raise ValueError('Audio blocks are missing; keep the page open and retry upload')
        s.segmenter.finish()
        c['live'].update(raw_complete=True,expected_parts=expected,total_samples=total,capture_interrupted=bool(interrupted))
        c.update(state='finishing',audio_file='raw.wav',ended_at=store.now(),error=None);store.save(c)
    SESSIONS.pop(cid,None)
    kick(cid)
    return status(cid)

def combine(items):
    result=[];previous=None
    for item in items:
        if item['status'] not in ('done','manual'):continue
        text=item['text'].strip()
        if previous and item['overlap']:
            left=result[-1].split() if result else [];right=text.split()
            norm=lambda word:re.sub(r'[^\w]','',word).casefold()
            # Remove only exact multi-word overlap at a known overlapping audio boundary.
            for count in range(min(16,len(left),len(right)),1,-1):
                if [norm(w) for w in left[-count:]]==[norm(w) for w in right[:count]]:
                    text=' '.join(right[count:]);break
        if text:result.append(text)
        previous=item
    return '\n'.join(result)

def finalize(cid):
    with store.LOCK:
        c=store.load(cid);items=chunks(cid)
        if not c['live']['raw_complete'] or c['live']['structured_once']:return
        if any(i['status'] not in ('done','manual') for i in items):
            c.update(state='failed',error='Could not prepare this consultation. The complete recording is saved.');store.save(c);return
        transcript=combine(items)
        if not transcript.strip():
            c.update(state='no_speech',error='No speech detected. The complete raw recording is saved.');store.save(c);return
        (store.directory(cid)/'transcript.txt').write_text(transcript,encoding='utf-8')
        c['live']['structured_once']=True;c.update(state='queued',error=None);store.save(c)
        pipeline.submit(cid,True)

def transcriber(cid):
    if cid in TRANSCRIBERS:return TRANSCRIBERS[cid]
    with store.LOCK:
        c=store.load(cid);continuity=c['live'].get('continuity',{})
        if continuity.get('state') not in (None,'new') or any(i['status'] in ('done','manual','unknown') for i in chunks(cid)):
            raise ContinuityError('Previous OpenAI session is unavailable; no replacement session opened')
        c['live']['continuity']={'state':'opening','opened_at':store.now()};store.save(c)
    stream=RealtimeTranscriber(pipeline.get_key(),pipeline.SETTINGS['transcription_model'],TRANSCRIPTION_PROMPT)
    try:
        with store.LOCK:
            c=store.load(cid);c['live']['continuity'].update(state='open',session_id=stream.session_id,request_id=stream.request_id);store.save(c)
        TRANSCRIBERS[cid]=stream;return stream
    except Exception:
        stream.close();raise

def close_transcriber(cid,broken=False):
    stream=TRANSCRIBERS.pop(cid,None)
    if stream:stream.close()
    with store.LOCK:
        c=store.load(cid);continuity=c['live'].get('continuity')
        if continuity and (broken or continuity['state']=='open'):
            continuity.update(state='broken' if broken else 'closed',closed_at=store.now());store.save(c)

def work(cid):
    try:
        while True:
            with store.LOCK:
                pending=[i for i in chunks(cid) if i['status']=='queued']
                complete=store.load(cid)['live']['raw_complete']
                if pending:
                    item=pending[0]
                    if store.load(cid)['live'].get('continuity',{}).get('state') in ('broken','closed'):
                        item.update(status='failed',error='OpenAI session continuity was lost. This chunk was not sent; recording retained.')
                        save_chunk(cid,item);continue
                    item.update(status='sending',started_at=store.now());save_chunk(cid,item)
            if not pending:
                if complete:close_transcriber(cid)
                finalize(cid);return
            path=store.directory(cid)/'chunks'/f'{item["seq"]:06d}.wav'
            logdir=path.with_suffix('');logdir.mkdir(exist_ok=True);log=pipeline.CallLog(logdir)
            stream=None
            def on_item(session_id,item_id,previous_item_id):
                item.update(openai_session_id=session_id,openai_item_id=item_id,previous_item_id=previous_item_id)
                with store.LOCK:save_chunk(cid,item)
            try:
                stream=transcriber(cid)
                response=log.call('transcription',pipeline.SETTINGS['transcription_model'],lambda:stream.transcribe(path,on_item))
                log.calls[-1].update(transport='realtime_websocket',session_id=response.session_id,item_id=response.id,event_id=response.event_id,request_id_scope='websocket_handshake',sent_audio_seconds=response.sent_audio_seconds)
                item.update(status='done',text=response.text,error=None)
                # Persist clinical evidence before accounting or other non-clinical work.
                with store.LOCK:save_chunk(cid,item)
            except Exception as exc:
                item.update(status='failed',error=type(exc).__name__+' - continuity lost; not retried. Recording retained.',failure_reason=str(exc) if isinstance(exc,ContinuityError) else type(exc).__name__,provider_error=getattr(exc,'details',{}))
                close_transcriber(cid,broken=True)
            finally:
                if log.calls:
                    log.calls[-1].update(transport='realtime_websocket',session_id=stream.session_id if stream else None,item_id=item.get('openai_item_id'),request_id_scope='websocket_handshake',request_id=log.calls[-1].get('request_id') or (stream.request_id if stream else None))
                item['metrics']=log.finish();item['call_log']=log.document()
                with store.LOCK:save_chunk(cid,item)
    except Exception:
        close_transcriber(cid,broken=True)
        with store.LOCK:
            for pending in chunks(cid):
                if pending['status']=='sending':
                    pending.update(status='unknown',error='Could not persist the API outcome. Not resent; verify the saved audio.');save_chunk(cid,pending)
            finalize(cid)
    finally:
        with store.LOCK:
            RUNNING.discard(cid)
            current=store.load(cid)
            needs_finalization=current['live']['raw_complete'] and current['state']=='finishing' and not current['live']['structured_once']
            if needs_finalization or any(i['status']=='queued' for i in chunks(cid)):kick(cid)

def kick(cid):
    with store.LOCK:
        if cid in RUNNING:return
        RUNNING.add(cid);POOL.submit(work,cid)

def status(cid):
    c=store.load(cid)
    if not c.get('live'):return c
    items=chunks(cid)
    with store.connection() as db:parts=db.execute('SELECT seq,length(pcm) FROM stream_parts WHERE cid=? ORDER BY seq',(cid,)).fetchall()
    next_seq=0
    for seq,_ in parts:
        if seq!=next_seq:break
        next_seq+=1
    rate=c['live']['sample_rate'];raw_samples=sum(length//2 for _,length in parts)
    c['live'].update(received_parts=len(parts),next_part=next_seq,raw_seconds=raw_samples/rate,sent_audio_seconds=sum((i['end_sample']-i['start_sample'])/rate for i in items if i['status']!='queued'),chunks=items,transcript=combine(items))
    calls=[call for i in items for call in i.get('call_log',{}).get('calls',[])]+c.get('metrics',{}).get('calls',[])
    if calls:
        costs=[(call.get('estimated_cost') or {}).get('amount') for call in calls]
        c['metrics']={'elapsed_seconds':sum(call.get('elapsed_seconds') or 0 for call in calls),'estimated_cost_usd':sum(costs) if all(v is not None for v in costs) else None,'calls':calls}
    return c

def recover():
    init()
    for workspace in ('clinic','hospital'):
        for summary in store.list_all(workspace):
            c=store.load(summary['id'])
            if not c.get('live'):continue
            cid=c['id']
            with store.LOCK:
                continuity=c['live'].get('continuity',{})
                if continuity.get('state') in ('open','opening'):
                    continuity.update(state='broken',closed_at=store.now());store.save(c)
                for item in chunks(cid):
                    if item['status']=='sending':
                        item.update(status='unknown',error='Server restarted during API request; not resent to avoid duplicate billing.');save_chunk(cid,item)
                if not c['live']['raw_complete']:
                    c.update(state='capture_interrupted',error='Capture interrupted. Reconnect saved audio or end the saved portion.');store.save(c)
                elif c['live']['structured_once'] and c['state'] in ('queued','structuring','failed'):
                    c.update(state='failed',error='Clinical request was interrupted and will not be retried. Source recording and transcript are retained.');store.save(c)
            if not c['live']['structured_once']:kick(cid)
