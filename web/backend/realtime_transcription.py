"""One transcription-only WebSocket per consultation. Never reconnect or replay audio."""
import audioop
import base64
import json
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).parent/'vendor'))
from websockets.sync.client import connect

class ContinuityError(RuntimeError):
    def __init__(self,message,details=None):
        super().__init__(message);self.details=details or {}

class RealtimeTranscriber:
    def __init__(self,key,model,prompt):
        self.socket=None;self.session_id=None;self.previous_item_id=None;self.request_id=None;self.model=model
        try:
            self.socket=connect('wss://api.openai.com/v1/realtime?intent=transcription',additional_headers={'Authorization':'Bearer '+key},open_timeout=20,close_timeout=2,max_size=4_000_000,ping_interval=20,ping_timeout=20)
            headers=getattr(getattr(self.socket,'response',None),'headers',{})
            self.request_id=headers.get('x-request-id')
            created=self.wait_for({'session.created','transcription_session.created'},time.monotonic()+20)
            self.session_id=created['session']['id']
            self.send({'type':'session.update','session':{'type':'transcription','audio':{'input':{'format':{'type':'audio/pcm','rate':24000},'transcription':{'model':model,'prompt':prompt,'languages':['ur','en']},'turn_detection':None}}}})
            updated=self.wait_for({'session.updated','transcription_session.updated'},time.monotonic()+20)
            session=updated['session'];config=session.get('audio',{}).get('input',{})
            if session.get('id')!=self.session_id or session.get('type')!='transcription' or config.get('transcription',{}).get('model')!=model or config.get('turn_detection') is not None:
                raise ContinuityError('Unexpected transcription session configuration')
        except Exception:
            self.close();raise
    def send(self,event):
        self.socket.send(json.dumps({'event_id':'evt_'+uuid4().hex,**event}))
    def receive(self,deadline):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Transcription event timed out')
        event=json.loads(self.socket.recv(timeout=remaining))
        if event.get('type')=='error':raise ContinuityError('OpenAI rejected a transcription session event', {k:event.get('error',{}).get(k) for k in ('type','code','param','event_id')})
        return event
    def wait_for(self,types,deadline):
        while True:
            event=self.receive(deadline)
            if event.get('type') in types:return event
    def transcribe(self,path,on_item):
        with wave.open(str(path),'rb') as audio:
            if audio.getnchannels()!=1 or audio.getsampwidth()!=2:raise ValueError('Expected mono PCM16 audio')
            rate=audio.getframerate();pcm=audio.readframes(audio.getnframes())
        if rate!=24000:pcm,_=audioop.ratecv(pcm,2,1,rate,24000,None)
        # The Realtime commit buffer requires at least 100 ms, including a tiny final turn.
        pcm=pcm.ljust(4800,b'\0')
        self.sent_audio_seconds=len(pcm)/48000
        for offset in range(0,len(pcm),48000):
            self.send({'type':'input_audio_buffer.append','audio':base64.b64encode(pcm[offset:offset+48000]).decode('ascii')})
        self.send({'type':'input_audio_buffer.commit'})
        deadline=time.monotonic()+120;item_id=None;early={}
        while True:
            event=self.receive(deadline);kind=event.get('type')
            if kind=='input_audio_buffer.committed':
                if item_id is not None:raise ContinuityError('Unexpected extra committed turn')
                item_id=event['item_id']
                if event.get('previous_item_id')!=self.previous_item_id:raise ContinuityError('OpenAI turn chain does not match this consultation')
                on_item(self.session_id,item_id,self.previous_item_id)
                event=early.pop(item_id,None)
                if event is None:continue
                kind=event['type']
            if kind not in ('conversation.item.input_audio_transcription.completed','conversation.item.input_audio_transcription.failed'):continue
            if item_id is None:
                if len(early)>=16:raise ContinuityError('Unexpected unmatched transcription events')
                early[event.get('item_id')]=event;continue
            if event.get('item_id')!=item_id:continue
            if kind.endswith('.failed'):raise ContinuityError('OpenAI could not transcribe the committed turn', {k:event.get('error',{}).get(k) for k in ('type','code','param')})
            text=event.get('transcript','').strip()
            # A completed empty turn is valid: retain its place in the session chain.
            self.previous_item_id=item_id
            return SimpleNamespace(text=text,model=event.get('model'),usage=event.get('usage'),service_tier=event.get('service_tier'),_request_id=self.request_id,id=item_id,event_id=event.get('event_id'),session_id=self.session_id,sent_audio_seconds=self.sent_audio_seconds)
    def close(self):
        if self.socket is not None:
            try:self.socket.close()
            except Exception:pass
            self.socket=None
