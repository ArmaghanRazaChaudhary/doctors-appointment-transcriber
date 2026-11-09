"use client";
import { useEffect,useRef,useState } from 'react';
import { Square,AudioLines,LoaderCircle,AlertCircle,Play } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Consultation } from '@/lib/clinical';
import { toast } from 'sonner';
export function LiveConsultation({consultation:c,token,recording,ending,seconds,onEnd}:{consultation:Consultation;token:string;recording:boolean;ending:boolean;seconds:number;onEnd:()=>void;onUpdate:(c:Consultation)=>void}){
 const [audio,setAudio]=useState(''),[loadingAudio,setLoadingAudio]=useState(false);
 const player=useRef<HTMLAudioElement>(null),live=c.live!;
 const failed=['failed','transcription_incomplete','no_speech'].includes(c.state);
 const transcribing=live.chunks.some(i=>['queued','sending'].includes(i.status));
 const busy=!failed&&(ending||transcribing||['finishing','queued','structuring','transcribing'].includes(c.state));
 const title=failed?'Consultation could not be completed':c.state==='structuring'||c.state==='queued'?'Preparing clinical note':ending?'Saving recording':!recording&&c.state==='finishing'?'Finishing transcription':transcribing||c.state==='transcribing'?'Transcribing conversation':recording?'Recording consultation':'Recording paused';
 const detail=failed?'Your recording is saved. Processing stopped before a clinical note was created.':c.state==='structuring'||c.state==='queued'?'Preparing your clinical draft from the complete conversation.':ending?'Saving the final part of your recording.':busy?'Your conversation is being transcribed.':recording?'Microphone is recording. Speak naturally; the transcript will appear here.':'Your saved recording is retained.';
 useEffect(()=>{if(!audio)return;void player.current?.play().catch(()=>toast('Press Play on the recording below.'));return()=>URL.revokeObjectURL(audio);},[audio]);
 async function listen(){
  if(audio){await player.current?.play().catch(()=>toast('Press Play on the recording below.'));return;}
  setLoadingAudio(true);
  try{const response=await fetch('/api/consultations/'+c.id+'/raw-audio',{headers:{'X-Suno-Token':token}});if(!response.ok)throw Error('Could not load the saved recording');setAudio(URL.createObjectURL(await response.blob()));}catch(e){toast.error(String(e));}finally{setLoadingAudio(false);}
 }
 return <section className="live-consultation"><div className="live-heading"><div>{failed?<AlertCircle className="status-error"/>:busy?<LoaderCircle className="progress-spinner" aria-label="Processing"/>:<AudioLines className={recording?'recording-pulse':''}/>}<h2>{title}</h2></div>{recording&&<Button disabled={ending} onClick={onEnd}>{ending?<LoaderCircle className="progress-spinner"/>:<Square/>}{ending?'Saving…':'End consultation'} · {Math.floor(seconds/60)}:{String(seconds%60).padStart(2,'0')}</Button>}</div><div className="consultation-progress" role={failed?'alert':'status'} aria-live="polite"><p>{detail}</p><span>{Math.floor((live.raw_seconds||0)/60)}:{String(Math.floor((live.raw_seconds||0)%60)).padStart(2,'0')} recording saved</span></div>{live.transcript?<div className="live-transcript" dir="auto">{live.transcript}</div>:<div className="transcript-placeholder">{busy&&<LoaderCircle className="progress-spinner" aria-hidden="true"/>}<span>{failed?'Transcript unavailable. The original recording is safe.':c.state==='structuring'||c.state==='queued'?'Preparing clinical note…':ending?'Saving recording…':busy?'Transcription in progress…':recording?'Listening and recording…':'Recording is paused.'}</span></div>}{live.raw_complete&&<Button variant="outline" disabled={loadingAudio} onClick={listen}>{loadingAudio?<LoaderCircle className="progress-spinner"/>:<Play/>}{loadingAudio?'Loading recording…':'Play saved recording'}</Button>}{audio&&<audio ref={player} controls autoPlay src={audio} aria-label="Complete consultation recording" onError={()=>toast.error('Could not play this recording.')}/>}</section>;
}
