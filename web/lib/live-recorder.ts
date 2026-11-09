import type { Consultation } from './clinical';
export type CaptureMeta={id:string;rate:number;parts:number;samples:number;stopped:boolean;interrupted?:boolean};
type Block={key:string;id:string;seq:number;pcm:ArrayBuffer};
let database:Promise<IDBDatabase>|undefined;
function db(){return database??=new Promise<IDBDatabase>((resolve,reject)=>{const r=indexedDB.open('suno-capture',1);r.onupgradeneeded=()=>{r.result.createObjectStore('sessions',{keyPath:'id'});r.result.createObjectStore('parts',{keyPath:'key'});};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});}
async function transaction<T>(stores:string[],mode:IDBTransactionMode,operation:(tx:IDBTransaction)=>IDBRequest<T>){const d=await db();return new Promise<T>((resolve,reject)=>{const tx=d.transaction(stores,mode);const r=operation(tx);tx.oncomplete=()=>resolve(r.result);tx.onabort=()=>reject(tx.error||Error('Could not save microphone audio locally'));tx.onerror=()=>reject(tx.error);});}
export async function recoverable(){return transaction<CaptureMeta[]>(['sessions'],'readonly',tx=>tx.objectStore('sessions').getAll());}
async function saveMeta(meta:CaptureMeta){await transaction(['sessions'],'readwrite',tx=>tx.objectStore('sessions').put(meta));}
async function blocks(id:string){const all=await transaction<Block[]>(['parts'],'readonly',tx=>tx.objectStore('parts').getAll());return all.filter(b=>b.id===id).sort((a,b)=>a.seq-b.seq);}
export class LiveRecorder {
  meta:CaptureMeta;token:string;stream?:MediaStream;context?:AudioContext;node?:AudioWorkletNode;
  private writes=Promise.resolve();private pumping:Promise<void>|null=null;private stopped?:()=>void;private closing=false;private captureError:Error|null=null;
  constructor(meta:CaptureMeta,token:string,private notify:(message:string)=>void){this.meta=meta;this.token=token;}
  async request(action:string,body:BodyInit,json=false){
    const response=await fetch('/api/consultations/'+this.meta.id+'/'+action,{method:'POST',headers:{'X-Suno-Token':this.token,'X-Recording-Consent':'yes','Content-Type':json?'application/json':'application/octet-stream'},body,signal:AbortSignal.timeout(20000)});
    if(response.status===403){const s=await fetch('/api/session').then(r=>r.json()) as {token:string};this.token=s.token;throw Error('Reconnected to local server; resume upload.');}
    const data=await response.json() as Consultation&{error?:string};if(!response.ok)throw Error(data.error||'Audio upload failed');return data;
  }
  async start(){
    if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia)throw Error('Open localhost in Chrome or Edge and allow microphone access.');
    try {
      this.stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:false,noiseSuppression:false,autoGainControl:false}});
      this.context=new AudioContext({sampleRate:48000});this.meta.rate=this.context.sampleRate;
      await this.context.audioWorklet.addModule('/pcm-recorder.js');
      await saveMeta(this.meta);
      await this.request('live-start',JSON.stringify({sample_rate:this.meta.rate}),true);await saveMeta(this.meta);
      this.node=new AudioWorkletNode(this.context,'pcm-recorder');
      this.node.port.onmessage=e=>{
        if(e.data.type==='stopped'){this.stopped?.();return;}
        if(e.data.type!=='pcm')return;
        const pcm=e.data.buffer as ArrayBuffer,seq=this.meta.parts++;this.meta.samples+=pcm.byteLength/2;
        const meta={...this.meta};
        this.writes=this.writes.then(async()=>{await transaction(['sessions','parts'],'readwrite',tx=>{tx.objectStore('sessions').put(meta);return tx.objectStore('parts').put({key:meta.id+':'+String(seq).padStart(8,'0'),id:meta.id,seq,pcm});});}).catch(e=>{this.captureError=e instanceof Error?e:Error(String(e));this.notify('Local audio storage failed. Stop this consultation; the complete recording cannot be verified.');void this.abortCapture();throw e;});
        void this.writes.then(()=>this.pump()).catch(()=>{});
      };
      this.node.onprocessorerror=()=>{this.captureError=Error('Microphone processor failed');this.notify('Microphone capture failed. End the consultation to retain saved audio.');};
      this.stream.getAudioTracks().forEach(t=>{t.onended=()=>{if(!this.closing){this.meta.interrupted=true;this.notify('Microphone disconnected. End the consultation to save the captured portion.');}};});
      const source=this.context.createMediaStreamSource(this.stream),mute=this.context.createGain();mute.gain.value=0;
      source.connect(this.node);this.node.connect(mute);mute.connect(this.context.destination);await this.context.resume();
      this.context.onstatechange=()=>{if(!this.closing&&this.context?.state==='suspended'){this.meta.interrupted=true;this.notify('Audio capture paused by the browser. End the consultation; a gap may exist.');}};
    }catch(e){await this.abortCapture();
      try{const response=await fetch('/api/consultations/'+this.meta.id,{headers:{'X-Suno-Token':this.token}});const c=await response.json() as Consultation;
        if(response.ok&&c.state==='ready')await transaction(['sessions'],'readwrite',tx=>tx.objectStore('sessions').delete(this.meta.id));
      }catch{}throw e;}
  }
  private async pump(){
    if(this.pumping)return this.pumping;
    this.pumping=(async()=>{for(;;){const pending=await blocks(this.meta.id);if(!pending.length)return;const block=pending[0];let sent=false;
      for(let attempt=0;attempt<3;attempt++){try{await this.request('live-part?seq='+block.seq,block.pcm);sent=true;break;}catch(e){if(attempt===2){this.notify('Upload paused. Audio is buffered on this device; End consultation or Resume saved upload will reconnect.');return;}await new Promise(r=>setTimeout(r,500*(attempt+1)));}}
      if(sent)await transaction(['parts'],'readwrite',tx=>tx.objectStore('parts').delete(block.key));
    }})().finally(()=>{this.pumping=null;});return this.pumping;
  }
  private async abortCapture(){this.closing=true;this.stream?.getTracks().forEach(t=>t.stop());this.node?.disconnect();if(this.context&&this.context.state!=='closed')await this.context.close();}
  async end(recovery=false){
    if(!this.closing&&this.node){this.closing=true;await new Promise<void>((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Microphone did not flush. Saved audio remains available for recovery.')),5000);this.stopped=()=>{clearTimeout(timer);resolve();};this.node!.port.postMessage('stop');}).finally(()=>this.abortCapture());}
    await this.writes;if(this.captureError)throw this.captureError;
    // Recover a start whose server response was lost before any audio arrived.
    const check=await fetch('/api/consultations/'+this.meta.id,{headers:{'X-Suno-Token':this.token}});
    if(check.ok){const existing=await check.json() as Consultation;if(existing.state==='ready')await this.request('live-start',JSON.stringify({sample_rate:this.meta.rate}),true);}
    this.meta.stopped=true;if(recovery)this.meta.interrupted=true;await saveMeta(this.meta);await this.pump();
    if((await blocks(this.meta.id)).length)throw Error('Some audio is still on this device. Keep this page open and use Resume saved upload.');
    const result=await this.request('live-end',JSON.stringify({expected_parts:this.meta.parts,total_samples:this.meta.samples,interrupted:Boolean(this.meta.interrupted)}),true);
    await transaction(['sessions'],'readwrite',tx=>tx.objectStore('sessions').delete(this.meta.id));return result;
  }
}
