"""Streaming correctness tests with fake OpenAI only; no network provider calls."""
import base64
from collections import deque
import copy
import json
import struct
import time
import unittest
import wave
from unittest.mock import patch
from types import SimpleNamespace
import test_flow as flow
import streaming
import realtime_transcription
from segmentation import PauseSegmenter

AUDIO_CALLS=[]
FAIL_AUDIO=False
SOCKETS=[]
class FakeSocket:
    def __init__(self):
        self.id='offline-session-'+str(len(SOCKETS)+1);self.previous=None;self.closed=False;self.sent=[];self.buffer=b''
        self.response=SimpleNamespace(headers={'x-request-id':'offline-handshake-'+self.id})
        self.events=deque([{'type':'session.created','session':{'id':self.id}}]);SOCKETS.append(self)
    def send(self,payload):
        event=json.loads(payload);self.sent.append(event)
        if event['type']=='session.update':
            assert event['session']['audio']['input']['transcription']['languages']==['ur','en']
            self.events.append({'type':'session.updated','session':{'id':self.id,**event['session']}})
        elif event['type']=='input_audio_buffer.append':self.buffer+=base64.b64decode(event['audio'])
        elif event['type']=='input_audio_buffer.commit':
            assert len(self.buffer)>=4800 and len(self.buffer)%2==0
            AUDIO_CALLS.append((self.id,len(self.buffer)));self.buffer=b''
            item='offline-item-'+str(len(AUDIO_CALLS))
            self.events.append({'type':'input_audio_buffer.committed','item_id':item,'previous_item_id':self.previous})
            # Unrelated events must never be assigned to this consultation's current chunk.
            self.events.append({'type':'conversation.item.input_audio_transcription.completed','item_id':'unrelated','transcript':'NEVER USE THIS'})
            self.events.append({'type':'conversation.item.input_audio_transcription.'+('failed' if FAIL_AUDIO else 'completed'),'item_id':item,'transcript':'Test speech '+str(len(AUDIO_CALLS)),'model':'gpt-transcribe','usage':{'type':'duration','seconds':1},'event_id':'event-'+item})
            self.previous=item
    def recv(self,timeout=None):
        if self.closed or not self.events:raise TimeoutError('Offline socket disconnected')
        return json.dumps(self.events.popleft())
    def close(self):self.closed=True

def fake_connect(url,**options):
    assert url=='wss://api.openai.com/v1/realtime?intent=transcription'
    assert options['additional_headers']['Authorization']=='Bearer offline-test-key'
    return FakeSocket()
realtime_transcription.connect=fake_connect
streaming.init()
def wait(cid):
    for _ in range(300):
        c=streaming.status(cid)
        settled=cid not in streaming.RUNNING and c['state'] not in ('finishing','queued','structuring')
        accounting_ready=c['state']!='review_required' or any(i['stage']=='clinical_structuring' for i in c.get('metrics',{}).get('calls',[]))
        if settled and accounting_ready:return c
        time.sleep(.02)
    raise AssertionError('Processing did not settle: '+str(c))
def start():
    c=flow.create();url='/api/consultations/'+c['id']
    code,result=flow.request(url+'/live-start',{'sample_rate':16000},{'X-Recording-Consent':'yes'})
    assert code==200,result
    return c['id'],url
VOICE=struct.pack('<h',1000)*1600
SILENCE=b'\0\0'*8000

class StreamingFlow(unittest.TestCase):
    def setUp(self):
        self.vad=patch.object(PauseSegmenter,'voiced',lambda self,frame:any(frame));self.vad.start()
    def tearDown(self):self.vad.stop()
    def test_thirty_chunks_one_consultation_and_one_clinical_call(self):
        before=len(AUDIO_CALLS);clinical=flow.COUNTS['clinical'];connections=len(SOCKETS);cid,url=start()
        raw=(VOICE+SILENCE)*30
        blocks=[raw[i:i+16000] for i in range(0,len(raw),16000)]
        order=list(range(len(blocks)));order[:2]=[1,0]
        for seq in order:
            code,data=flow.request(url+'/live-part?seq='+str(seq),blocks[seq]);self.assertEqual(code,200,data)
        self.assertEqual(flow.COUNTS['clinical'],clinical)
        self.assertEqual(flow.request(url+'/live-part?seq=0',blocks[0])[0],200)
        self.assertEqual(flow.request(url+'/live-part?seq=0',b'\0\0')[0],400)
        self.assertEqual(flow.request(url+'/live-end',{'expected_parts':len(blocks)+1,'total_samples':len(raw)//2})[0],400)
        manifest={'expected_parts':len(blocks),'total_samples':len(raw)//2}
        self.assertEqual(flow.request(url+'/live-end',manifest)[0],200)
        c=wait(cid);self.assertEqual(c['state'],'review_required',c.get('error'))
        self.assertEqual(len(streaming.chunks(cid)),30)
        self.assertEqual(len(SOCKETS)-connections,1)
        self.assertTrue(SOCKETS[-1].closed)
        self.assertEqual(len({i['openai_session_id'] for i in streaming.chunks(cid)}),1)
        self.assertNotIn('NEVER USE THIS',c['live']['transcript'])
        self.assertTrue(all(i['call_log']['calls'][0]['transport']=='realtime_websocket' for i in streaming.chunks(cid)))
        self.assertEqual(len(AUDIO_CALLS)-before,30)
        self.assertEqual(flow.COUNTS['clinical']-clinical,1)
        self.assertEqual(len(c['metrics']['calls']),31)
        self.assertTrue(all(i['request_id'] for i in c['metrics']['calls']))
        with wave.open(str(flow.store.directory(cid)/'raw.wav'),'rb') as f:self.assertEqual(f.readframes(f.getnframes()),raw)
        self.assertEqual(flow.request(url+'/live-end',manifest)[0],200)
        self.assertEqual(flow.request(url+'/live-part?seq=0',blocks[0])[0],200)
        self.assertEqual(flow.COUNTS['clinical']-clinical,1)
        self.assertEqual(len(AUDIO_CALLS)-before,30)
    def test_failure_requires_manual_correction_never_paid_retry(self):
        global FAIL_AUDIO
        cid,url=start();before=len(AUDIO_CALLS);clinical=flow.COUNTS['clinical'];FAIL_AUDIO=True
        try:
            flow.request(url+'/live-part?seq=0',VOICE+SILENCE)
            flow.request(url+'/live-end',{'expected_parts':1,'total_samples':9600})
            c=wait(cid);self.assertEqual(c['state'],'failed')
        finally:FAIL_AUDIO=False
        self.assertEqual(flow.COUNTS['clinical'],clinical)
        self.assertEqual(flow.request(url+'/retry',{'confirm_cost':True})[0],409)
        self.assertEqual(flow.request(url+'/chunk-text',{'seq':0,'text':'A doctor must not transcribe audio chunks'})[0],410)
        self.assertEqual(len(AUDIO_CALLS)-before,1)
        self.assertEqual(flow.COUNTS['clinical'],clinical)
    def test_recovery_marks_inflight_unknown_and_never_resends(self):
        cid,url=start();before=len(AUDIO_CALLS)
        with patch.object(streaming,'kick'):
            flow.request(url+'/live-part?seq=0',VOICE+SILENCE)
            item=streaming.chunks(cid)[0];item['status']='sending';streaming.save_chunk(cid,item)
            flow.request(url+'/live-end',{'expected_parts':1,'total_samples':9600})
        streaming.SESSIONS.clear();streaming.recover();c=wait(cid)
        self.assertEqual(streaming.chunks(cid)[0]['status'],'unknown')
        self.assertEqual(c['state'],'failed');self.assertEqual(len(AUDIO_CALLS),before)
    def test_restart_reconstructs_raw_and_unsent_chunk_only(self):
        cid,url=start();raw=VOICE+SILENCE;before=len(AUDIO_CALLS)
        with patch.object(streaming,'kick'):
            flow.request(url+'/live-part?seq=0',raw[:6000])
            streaming.SESSIONS.clear()
            flow.request(url+'/live-part?seq=1',raw[6000:])
            self.assertEqual(len(streaming.chunks(cid)),1)
        flow.request(url+'/live-end',{'expected_parts':2,'total_samples':9600,'interrupted':True});c=wait(cid)
        self.assertEqual(c['state'],'review_required');self.assertEqual(len(AUDIO_CALLS)-before,1)
        with wave.open(str(flow.store.directory(cid)/'raw.wav'),'rb') as f:self.assertEqual(f.readframes(f.getnframes()),raw)
        self.assertTrue(any('interrupted' in w for w in c['result']['validation_warnings']))
    def test_silence_preserved_without_api_calls(self):
        cid,url=start();before=len(AUDIO_CALLS);clinical=flow.COUNTS['clinical']
        flow.request(url+'/live-part?seq=0',SILENCE)
        flow.request(url+'/live-end',{'expected_parts':1,'total_samples':8000})
        self.assertEqual(wait(cid)['state'],'no_speech')
        self.assertEqual(len(AUDIO_CALLS),before);self.assertEqual(flow.COUNTS['clinical'],clinical)
    def test_sessions_are_isolated_and_stay_open_between_pauses(self):
        before=len(SOCKETS);a,ua=start();b,ub=start()
        flow.request(ua+'/live-part?seq=0',VOICE+SILENCE);wait(a)
        first=streaming.chunks(a)[0];self.assertFalse(SOCKETS[-1].closed)
        flow.request(ub+'/live-part?seq=0',VOICE+SILENCE);wait(b)
        flow.request(ua+'/live-part?seq=1',VOICE+SILENCE);wait(a)
        second=streaming.chunks(a)[1]
        self.assertEqual(first['openai_session_id'],second['openai_session_id'])
        self.assertEqual(second['previous_item_id'],first['openai_item_id'])
        self.assertNotEqual(first['openai_session_id'],streaming.chunks(b)[0]['openai_session_id'])
        self.assertEqual(len(SOCKETS)-before,2)
        flow.request(ua+'/live-end',{'expected_parts':2,'total_samples':19200});wait(a)
        flow.request(ub+'/live-end',{'expected_parts':1,'total_samples':9600});wait(b)
    def test_lost_session_does_not_reconnect_or_replay(self):
        cid,url=start();flow.request(url+'/live-part?seq=0',VOICE+SILENCE);wait(cid)
        before=len(SOCKETS);calls=len(AUDIO_CALLS)
        streaming.TRANSCRIBERS.pop(cid).close();streaming.recover();wait(cid)
        flow.request(url+'/live-part?seq=1',VOICE+SILENCE)
        flow.request(url+'/live-end',{'expected_parts':2,'total_samples':19200});c=wait(cid)
        self.assertEqual(c['live']['continuity']['state'],'broken')
        self.assertEqual(c['state'],'failed')
        self.assertEqual(streaming.chunks(cid)[1]['status'],'failed')
        self.assertEqual(len(SOCKETS),before);self.assertEqual(len(AUDIO_CALLS),calls)
    def test_wrong_session_chain_is_rejected_without_replay(self):
        cid,url=start();flow.request(url+'/live-part?seq=0',VOICE+SILENCE);wait(cid)
        stream=streaming.TRANSCRIBERS[cid];stream.socket.previous='wrong-consultation-item'
        before=len(SOCKETS);calls=len(AUDIO_CALLS)
        flow.request(url+'/live-part?seq=1',VOICE+SILENCE)
        flow.request(url+'/live-end',{'expected_parts':2,'total_samples':19200});c=wait(cid)
        self.assertEqual(c['state'],'failed')
        self.assertEqual(c['live']['continuity']['state'],'broken')
        self.assertEqual(len(SOCKETS),before);self.assertEqual(len(AUDIO_CALLS)-calls,1)
        self.assertEqual(streaming.chunks(cid)[1]['text'],'')
    def test_early_completion_waits_for_matching_commit(self):
        cid,url=start();before=len(SOCKETS)
        original=FakeSocket.send
        def reorder(socket,payload):
            original(socket,payload)
            if json.loads(payload)['type']=='input_audio_buffer.commit':socket.events.rotate(1)
        with patch.object(FakeSocket,'send',reorder):
            flow.request(url+'/live-part?seq=0',VOICE+SILENCE)
            flow.request(url+'/live-end',{'expected_parts':1,'total_samples':9600});c=wait(cid)
        self.assertEqual(c['state'],'review_required');self.assertEqual(len(SOCKETS)-before,1)
        self.assertNotIn('NEVER USE THIS',c['live']['transcript'])
    def test_empty_completed_turn_preserves_continuity(self):
        cid,url=start();before=len(SOCKETS)
        original=FakeSocket.send
        def empty_first(socket,payload):
            original(socket,payload)
            if json.loads(payload)['type']=='input_audio_buffer.commit':
                for event in socket.events:
                    if event.get('type')=='conversation.item.input_audio_transcription.completed':event['transcript']=''
        with patch.object(FakeSocket,'send',empty_first):
            flow.request(url+'/live-part?seq=0',VOICE+SILENCE);wait(cid)
        first=streaming.chunks(cid)[0]
        self.assertEqual(first['status'],'done');self.assertEqual(first['text'],'')
        flow.request(url+'/live-part?seq=1',VOICE+SILENCE)
        flow.request(url+'/live-end',{'expected_parts':2,'total_samples':19200});c=wait(cid)
        self.assertEqual(c['state'],'review_required')
        self.assertEqual(len(SOCKETS)-before,1)
        self.assertEqual(streaming.chunks(cid)[1]['previous_item_id'],first['openai_item_id'])
    def test_usage_export_failure_does_not_discard_transcript(self):
        from pathlib import Path
        real_open=Path.open
        def denied_export(path,*args,**kwargs):
            if path.name=='calls.json':raise PermissionError('Simulated Windows usage export failure')
            return real_open(path,*args,**kwargs)
        cid,url=start();before=len(AUDIO_CALLS);clinical=flow.COUNTS['clinical']
        with patch.object(Path,'open',denied_export):
            flow.request(url+'/live-part?seq=0',VOICE+SILENCE)
            flow.request(url+'/live-end',{'expected_parts':1,'total_samples':9600});c=wait(cid)
        self.assertEqual(c['state'],'review_required',c.get('error'))
        self.assertTrue(streaming.chunks(cid)[0]['text'])
        self.assertEqual(len(AUDIO_CALLS)-before,1)
        self.assertEqual(flow.COUNTS['clinical']-clinical,1)
        self.assertEqual(len(c['metrics']['calls']),2)
        with flow.store.connection() as db:
            self.assertTrue(db.execute('SELECT COUNT(*) FROM api_logs WHERE path LIKE ?',('%'+cid+'%',)).fetchone()[0]>=2)
    def test_pause_padding_partial_tail_and_forced_overlap(self):
        emitted=[];s=PauseSegmenter(16000,lambda *args:emitted.append(args))
        s.feed(SILENCE+VOICE+SILENCE[:-640]);self.assertEqual(emitted,[])
        s.feed(SILENCE[-640:]);self.assertEqual(len(emitted),1)
        self.assertEqual(emitted[0][2][-16000:],SILENCE)
        s.feed(b'\1\0'*13);s.finish();self.assertEqual(emitted[-1][2][-26:],b'\1\0'*13)
        emitted=[];s=PauseSegmenter(16000,lambda *args:emitted.append(args),max_seconds=1)
        s.feed(VOICE*10);self.assertEqual(len(emitted),1);s.finish();self.assertEqual(len(emitted),1)
        emitted=[];s=PauseSegmenter(16000,lambda *args:emitted.append(args),max_seconds=1)
        s.feed(VOICE*13);s.finish();self.assertEqual(len(emitted),2)
        self.assertEqual(emitted[0][2][-16000:],emitted[1][2][:16000]);self.assertTrue(emitted[1][3])

if __name__=='__main__':
    try:
        result=unittest.main(exit=False).result
        if not result.wasSuccessful():raise SystemExit(1)
    finally:flow.HTTP.shutdown();streaming.POOL.shutdown(wait=True);flow.pipeline.POOL.shutdown(wait=True)
