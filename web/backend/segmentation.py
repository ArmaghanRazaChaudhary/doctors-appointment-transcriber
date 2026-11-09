"""Conservative pause segmentation; every input sample is retained in raw audio."""
import collections
import math
import struct
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'vendor'))
import webrtcvad

class PauseSegmenter:
    def __init__(self,rate,emit,silence_ms=500,pre_ms=500,max_seconds=90):
        if rate not in (8000,16000,32000,48000):raise ValueError('Unsupported capture sample rate')
        self.rate=rate;self.emit=emit;self.frame_samples=rate//50;self.frame_bytes=self.frame_samples*2
        self.pause_frames=math.ceil(silence_ms/20);self.pad_frames=math.ceil(pre_ms/20)
        self.max_frames=math.ceil(max_seconds*50);self.vad=webrtcvad.Vad(0)
        self.pre=collections.deque(maxlen=self.pad_frames);self.active=[];self.remainder=b''
        self.frames=0;self.start=0;self.silent=0;self.overlap=False;self.finished=False;self.emitted_end=0
    def voiced(self,frame):
        return self.vad.is_speech(frame,self.rate)
    def feed(self,data):
        if self.finished:raise ValueError('Recording already ended')
        data=self.remainder+data;count=len(data)//self.frame_bytes
        for i in range(count):
            frame=data[i*self.frame_bytes:(i+1)*self.frame_bytes];self.frames+=1
            speech=self.voiced(frame)
            if not self.active:
                self.pre.append(frame)
                if speech:
                    self.active=list(self.pre);self.start=(self.frames-len(self.active))*self.frame_samples
                    self.pre.clear();self.silent=0;self.overlap=False
            else:
                self.active.append(frame);self.silent=0 if speech else self.silent+1
                if self.silent>=self.pause_frames:self.cut(False)
                elif len(self.active)>=self.max_frames:self.cut(True)
        self.remainder=data[count*self.frame_bytes:]
    def cut(self,forced):
        if not self.active:return
        audio=b''.join(self.active);end=self.start+len(audio)//2
        self.emit(self.start,end,audio,self.overlap,forced);self.emitted_end=end
        tail=self.active[-self.pad_frames:]
        if forced:
            self.active=tail;self.start=end-len(b''.join(tail))//2;self.overlap=True
        else:
            self.active=[];self.pre.extend(tail);self.overlap=False
        self.silent=0
    def finish(self):
        if self.finished:return
        self.finished=True
        # Keep the final partial frame, including a short word at Stop.
        tail=self.remainder
        if self.active:
            data=b''.join(self.active)+tail
            if self.start+len(data)//2>self.emitted_end:self.emit(self.start,self.start+len(data)//2,data,self.overlap,False)
        elif tail and any(tail):
            data=b''.join(self.pre)+tail;end=self.frames*self.frame_samples+len(tail)//2
            self.emit(end-len(data)//2,end,data,False,False)
        self.active=[];self.remainder=b''
