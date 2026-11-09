class PCMRecorder extends AudioWorkletProcessor {
  constructor(){super();this.buffer=new Int16Array(Math.round(sampleRate/4));this.used=0;this.total=0;this.stopped=false;this.port.onmessage=e=>{if(e.data==='stop'&&!this.stopped){this.stopped=true;this.flush();this.port.postMessage({type:'stopped',total:this.total});}};}
  flush(){if(!this.used)return;const pcm=this.buffer.slice(0,this.used);this.total+=this.used;this.port.postMessage({type:'pcm',buffer:pcm.buffer},[pcm.buffer]);this.used=0;}
  process(inputs){if(this.stopped)return false;const channels=inputs[0];if(!channels?.length)return true;for(let i=0;i<channels[0].length;i++){let sample=0;for(const channel of channels)sample+=channel[i]/channels.length;sample=Math.max(-1,Math.min(1,sample));this.buffer[this.used++]=sample<0?sample*32768:sample*32767;if(this.used===this.buffer.length)this.flush();}return true;}
}
registerProcessor('pcm-recorder',PCMRecorder);
