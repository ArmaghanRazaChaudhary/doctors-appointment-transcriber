"use client";

import { useEffect,useRef,useState } from 'react';

import { AudioLines,LoaderCircle,Check,ChevronRight,Mic,Plus,Printer,Square,Upload } from 'lucide-react';

import { Button } from '@/components/ui/button';

import { Input } from '@/components/ui/input';

import { Checkbox } from '@/components/ui/checkbox';

import { Tabs,TabsList,TabsTrigger } from '@/components/ui/tabs';

import { Dialog,DialogContent,DialogHeader,DialogTitle,DialogDescription } from '@/components/ui/dialog';

import { Toaster,toast } from 'sonner';

import { ClinicalSheet,Field } from '@/components/clinical-sheet';

import { PrintReport } from '@/components/print-report';

import { LiveRecorder,recoverable,CaptureMeta } from '@/lib/live-recorder';

import { LiveConsultation } from '@/components/live-consultation';

import { Consultation,Summary,ClinicalNote,activeStates,visitDate,visitState } from '@/lib/clinical';

type Session={token:string;key_ready:boolean;models:{transcription_model:string;clinical_model:string;reasoning_effort:string}};

export default function Home(){

 const [session,setSession]=useState<Session|null>(null),[error,setError]=useState(''),[mode,setMode]=useState('clinic'),[list,setList]=useState<Summary[]>([]),[c,setC]=useState<Consultation|null>(null),[dirty,setDirty]=useState(false),[saving,setSaving]=useState(false);

 const [newOpen,setNewOpen]=useState(false),[name,setName]=useState(''),[patientId,setPatientId]=useState(''),[doctor,setDoctor]=useState(''),[consent,setConsent]=useState(false),[recording,setRecording]=useState(false),[requesting,setRequesting]=useState(false),[seconds,setSeconds]=useState(0),[audio,setAudio]=useState<File|null>(null),[audioUrl,setAudioUrl]=useState(''),[uploading,setUploading]=useState(false);

 const [evidence,setEvidence]=useState<{transcript:string;original:unknown;original_transcript?:string|null;display_language?:string|null}|null>(null),[metrics,setMetrics]=useState(false),[leave,setLeave]=useState<{mode:string;id?:string}|null>(null);

 const live=useRef<LiveRecorder|null>(null);

 const [pending,setPending]=useState<CaptureMeta[]>([]),[captureWarning,setCaptureWarning]=useState('');

 const opening=useRef(false),loadSeq=useRef(0),uploadId=useRef<string|null>(null),busy=useRef(false);

 const processing=Boolean(c&&activeStates.includes(c.state));

 const stateRef=useRef<object>({});

 stateRef.current={workspace:mode,consultation_id:c?.id,patient:c?.patient,state:dirty?'edited':c?.state};

 useEffect(()=>{type Context={registerTool:(tool:{name:string;description:string;inputSchema:object;annotations:object;execute:(input:unknown)=>unknown},options:{signal:AbortSignal})=>void|Promise<void>};const context=(document as Document&{modelContext?:Context}).modelContext;if(!context)return;const lifecycle=new AbortController();try{void Promise.resolve(context.registerTool({name:'get_consultation_state',description:'Read the active consultation identity and workflow status. Does not record, send audio, or approve.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},execute(input){if(!input||typeof input!=='object'||Array.isArray(input)||Object.keys(input).length)throw Error('Expected an empty object');return structuredClone(stateRef.current);}},{signal:lifecycle.signal})).catch(()=>{});}catch{}return()=>lifecycle.abort();},[]);

 async function api<T=Consultation>(path:string,options:RequestInit={}):Promise<T>{const response=await fetch('/api'+path,{...options,headers:{'X-Suno-Token':session?.token||'',...(options.headers||{})}});const data=await response.json();if(!response.ok)throw Error((data as {error?:string}).error||'Request failed');return data as T;}

 useEffect(()=>{fetch('/api/session').then(r=>{if(!r.ok)throw Error();return r.json() as Promise<Session>;}).then(setSession).catch(()=>setError('Local server is unavailable. Start the app using start-local.ps1.'));return()=>{opening.current=false;live.current?.stream?.getTracks().forEach(t=>t.stop());};},[]);

 async function refresh(workspace=mode){const items=await api<Summary[]>('/consultations?workspace='+workspace) as Summary[];setList(items);return items;}

 async function select(id:string){const seq=++loadSeq.current;try{const next=await api('/consultations/'+id);if(seq===loadSeq.current){setC(next);setDirty(false);}}catch(e){toast.error(String(e));}}

 useEffect(()=>{if(!session)return;let active=true;const seq=++loadSeq.current;api<Summary[]>('/consultations?workspace='+mode).then(async items=>{if(!active)return;setList(items);if(items.length){const next=await api('/consultations/'+items[0].id);if(active&&seq===loadSeq.current){setC(next);setDirty(false);}}else setC(null);}).catch(e=>setError(String(e)));return()=>{active=false;};},[session,mode]);

 useEffect(()=>{if(!c||!processing)return;let alive=true;const id=c.id;const timer=setInterval(()=>{api('/consultations/'+id).then(next=>{if(!alive)return;setC(next);setList(items=>items.map(item=>item.id===next.id?{...item,state:next.state}:item));if(!activeStates.includes(next.state)){void refresh();if(next.state==='review_required')toast.success('Clinical draft ready');}}).catch(()=>{if(alive)setError('Connection interrupted. Processing may continue; reload to reconnect.');});},1200);return()=>{alive=false;clearInterval(timer);};},[c?.id,processing,session]);

 useEffect(()=>{if(!recording)return;const timer=setInterval(()=>setSeconds(s=>s+1),1000);return()=>clearInterval(timer);},[recording]);

 useEffect(()=>()=>{if(audioUrl)URL.revokeObjectURL(audioUrl);},[audioUrl]);

 useEffect(()=>{function warn(e:BeforeUnloadEvent){if(dirty||recording||uploading){e.preventDefault();e.returnValue='';}}window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);},[dirty,recording,uploading]);

 function move(workspace:string,id?:string){if(saving||uploading||recording)return;if(dirty){setLeave({mode:workspace,id});return;}if(workspace!==mode){setC(null);setMode(workspace);}else if(id)void select(id);}

 function change(note:ClinicalNote){if(!c?.result)return;setC({...c,result:{...c.result,note}});setDirty(true);}

 async function confirm(){if(!c?.result||busy.current)return;busy.current=true;setSaving(true);try{const next=await api('/consultations/'+c.id+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:c.revision,result:c.result,patient:c.patient,doctor:c.doctor,reviewed:true})});setC(next);setDirty(false);void refresh();toast.success('Reviewed consultation saved');}catch(e){toast.error(String(e));}finally{busy.current=false;setSaving(false);}}

 function choose(file?:File){if(!file)return;if(!/\.(wav|mp3|mp4|mpeg|mpga|m4a|webm|ogg|flac)$/i.test(file.name)||file.size>25_000_000||!file.size){toast.error('Choose a nonempty audio file under 25 MB.');return;}setAudio(file);setAudioUrl(URL.createObjectURL(file));uploadId.current=null;}

 async function startRecording(){

  if(!consent||busy.current||!name.trim()||!patientId.trim()||!doctor.trim())return;

  busy.current=true;setRequesting(true);setCaptureWarning('');

  try{const created=await api('/consultations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({patient:{name:name.trim(),patient_id:patientId.trim()},doctor:doctor.trim(),workspace:mode})});

   const capture=new LiveRecorder({id:created.id,rate:48000,parts:0,samples:0,stopped:false},session!.token,setCaptureWarning);live.current=capture;

   await capture.start();setC(await api('/consultations/'+created.id));setDirty(false);setRecording(true);setSeconds(0);setNewOpen(false);void refresh();

  }catch(e){toast.error(e instanceof Error?e.message:String(e));setPending(await recoverable());}finally{busy.current=false;setRequesting(false);}

 }

 async function endRecording(){if(!live.current||busy.current)return;busy.current=true;setUploading(true);

  try{const next=await live.current.end();setC(next);setCaptureWarning('');live.current=null;void refresh();}catch(e){toast.error(String(e));}finally{setRecording(false);setUploading(false);busy.current=false;setPending(await recoverable());}

 }

 async function resumeUpload(meta:CaptureMeta){if(busy.current||recording)return;busy.current=true;setUploading(true);

  try{const capture=new LiveRecorder(meta,session!.token,setCaptureWarning);const next=await capture.end(!meta.stopped);setC(next);setMode(next.workspace);setCaptureWarning('');void refresh(next.workspace);}catch(e){toast.error(String(e));}finally{setUploading(false);busy.current=false;setPending(await recoverable());}

 }

 useEffect(()=>{void recoverable().then(setPending).catch(()=>setCaptureWarning('Browser storage is unavailable. Microphone capture requires local browser storage.'));},[]);

 async function generate(){if(!audio||!consent||!name.trim()||!patientId.trim()||!doctor.trim()||busy.current)return;busy.current=true;setUploading(true);try{let id=uploadId.current;if(!id){const created=await api('/consultations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({patient:{name:name.trim(),patient_id:patientId.trim()},doctor:doctor.trim(),workspace:mode})});id=created.id;uploadId.current=id;}const next=await api('/consultations/'+id+'/audio?filename='+encodeURIComponent(audio.name),{method:'POST',headers:{'Content-Type':audio.type||'application/octet-stream','X-Recording-Consent':'yes'},body:audio});setC(next);setDirty(false);setNewOpen(false);opening.current=false;void refresh();setAudio(null);setAudioUrl('');uploadId.current=null;}catch(e){toast.error(String(e));}finally{busy.current=false;setUploading(false);}}

 async function viewEvidence(){if(!c)return;try{setEvidence(await api<{transcript:string;original:unknown;original_transcript?:string|null;display_language?:string|null}>('/consultations/'+c.id+'/evidence'));}catch(e){toast.error(String(e));}}

 function openNew(){if(recording||uploading||requesting){toast('End the current consultation first.');return;}if(pending.length){toast('Finish the saved audio upload before starting another consultation.');return;}if(dirty){toast('Confirm this consultation before starting another.');return;}setName('');setPatientId('');setAudio(null);setAudioUrl('');setConsent(false);uploadId.current=null;setNewOpen(true);}

 return <><div className="app-shell"><Toaster position="bottom-right"/><aside className="sidebar"><div className="brand"><AudioLines/>suno</div><small>Doctor’s assistant</small><div className="queue-heading"><span>Consultations</span><button aria-label="New consultation" onClick={openNew}><Plus size={19}/></button></div><nav>{list.map((v,i)=><button key={v.id} className={c?.id===v.id?'selected':''} disabled={saving} onClick={()=>move(mode,v.id)}><span className="queue-number">{String(i+1).padStart(2,'0')}</span><span><strong>{v.patient.name}</strong><small>{v.patient.patient_id}</small><small>{visitState(v.state)}</small></span><ChevronRight size={14}/></button>)}</nav><Button className="new-button" onClick={openNew}><Mic/>Start consultation</Button><div className="server-status"><span className={session?'connected':''}/>{session?'Local server connected':'Connecting…'}</div></aside><main className="workspace"><header className="patient-header"><div>{c?<><Field disabled={!c.result} label="Patient name" value={c.patient.name} onChange={v=>{setC({...c,patient:{...c.patient,name:v}});setDirty(true);}}/><div className="identity-meta"><span>Patient ID</span><Field disabled={!c.result} label="Patient ID" value={c.patient.patient_id} onChange={v=>{setC({...c,patient:{...c.patient,patient_id:v}});setDirty(true);}}/><span>{visitDate(c.created_at)}</span><span className="state-pill">{dirty?'Changes need review':visitState(c.state)}</span></div></>:<h1>Doctor’s assistant</h1>}<p>All clinical fields are editable — click to change</p></div><Tabs value={mode} onValueChange={v=>move(String(v))}><TabsList><TabsTrigger value="clinic">Clinic</TabsTrigger><TabsTrigger value="hospital">Hospital</TabsTrigger></TabsList></Tabs></header>

 {error&&<div className="error-banner" role="alert">{error}<button onClick={()=>location.reload()}>Reconnect</button></div>}{!session&&!error&&<div className="empty-state">Connecting to your local server…</div>}{session&&!session.key_ready&&<div className="error-banner">API key missing. Add your key to openai_trial/api-key.txt, then reload.</div>}

 {captureWarning&&<div className="error-banner" role="alert">{captureWarning}</div>}{!recording&&pending.map(meta=><div className="recovery-banner" key={meta.id}><span>Saved microphone audio awaiting completion · {Math.round(meta.samples/meta.rate)}s</span><Button disabled={uploading} onClick={()=>resumeUpload(meta)}>Resume saved upload</Button></div>)}

 {c?.live&&!c.result?<LiveConsultation consultation={c} token={session?.token||''} recording={recording} ending={uploading} seconds={seconds} onEnd={endRecording} onUpdate={setC}/>:c?.result?<><ClinicalSheet note={c.result.note} onChange={change} evidence={viewEvidence}/>{c.result.validation_warnings?.length>0&&<details className="validation"><summary>{c.result.validation_warnings.length} source verification flags</summary>{c.result.validation_warnings.map((v,i)=><p key={i}>{v}</p>)}</details>}<div className="confirm-bar"><div><label>Doctor<Field label="Doctor name" value={c.doctor} onChange={v=>{setC({...c,doctor:v});setDirty(true);}}/></label><small>Printed report excludes AI diagnosis</small>{c.metrics&&<button className="source-link" onClick={()=>setMetrics(true)}>Timing & usage · {c.metrics.elapsed_seconds.toFixed(1)}s</button>}</div>{c.state==='reviewed'&&!dirty?<Button onClick={()=>window.print()}><Printer/>Print report</Button>:<Button disabled={saving} onClick={confirm}><Check/>{saving?'Saving…':'Confirm / Reviewed'}</Button>}</div></>:session&&<div className="empty-state">{processing?<><LoaderCircle className="progress-spinner" size={42}/><h2>{c?.state==='transcribing'?'Transcribing conversation…':c?.state==='structuring'?'Preparing clinical note…':'Waiting to process…'}</h2><p>Your recording is saved. You can reload without submitting it again.</p></>:c?.state==='failed'?<><h2>Processing needs attention</h2><p>{c.error}</p><p>No paid retry was sent.</p><Button variant="outline" onClick={viewEvidence}>View saved transcript</Button></>:<><Mic size={42}/><h2>Start a consultation</h2><p>Record or upload audio, review the note, then print.</p><Button onClick={openNew}>New consultation</Button></>}</div>}</main>

 <Dialog open={newOpen} onOpenChange={v=>{if(recording||requesting||uploading)return;opening.current=v;setNewOpen(v);}}><DialogContent className="capture-dialog"><DialogHeader><DialogTitle>New consultation</DialogTitle><DialogDescription>Audio is saved locally and sent to OpenAI for transcription and clinical structuring.</DialogDescription></DialogHeader><div className="patient-form"><label>Patient name<Input value={name} maxLength={120} onChange={e=>{setName(e.target.value);uploadId.current=null;}} disabled={uploading}/></label><label>Patient ID<Input value={patientId} maxLength={120} onChange={e=>{setPatientId(e.target.value);uploadId.current=null;}} disabled={uploading}/></label><label>Doctor<Input value={doctor} maxLength={120} onChange={e=>{setDoctor(e.target.value);uploadId.current=null;}} disabled={uploading}/></label></div><label className="consent"><Checkbox checked={consent} onCheckedChange={v=>setConsent(Boolean(v))} disabled={recording||uploading}/>I have permission to record and process this consultation.</label><div className="capture-controls"><Button onClick={startRecording} disabled={!consent||!name.trim()||!patientId.trim()||!doctor.trim()||requesting||uploading||!session?.key_ready}><Mic/>{requesting?'Allow microphone…':'Start consultation'}</Button><label className="upload-control"><Upload size={17}/>Upload audio<input aria-label="Upload audio" type="file" accept=".wav,.mp3,.mp4,.mpeg,.mpga,.m4a,.webm,.ogg,.flac" disabled={recording||uploading||requesting} onChange={e=>choose(e.target.files?.[0])}/></label></div>{audio&&<div><small>{audio.name} · {(audio.size/1_000_000).toFixed(1)} MB</small><audio controls src={audioUrl}/></div>}<p className="models">{session?.models.transcription_model} + {session?.models.clinical_model} · {session?.models.reasoning_effort} reasoning<br/>Generating a note uses your OpenAI API credits.</p><Button onClick={generate} disabled={!audio||!consent||!name.trim()||!patientId.trim()||!doctor.trim()||uploading||recording||!session?.key_ready}>{uploading?'Saving audio…':'Generate clinical note'}</Button></DialogContent></Dialog>

 <Dialog open={Boolean(leave)} onOpenChange={v=>{if(!v)setLeave(null);}}><DialogContent><DialogHeader><DialogTitle>Unsaved clinical edits</DialogTitle><DialogDescription>Confirm the consultation to save your edits, or discard them to leave.</DialogDescription></DialogHeader><Button variant="outline" onClick={()=>setLeave(null)}>Keep editing</Button><Button onClick={()=>{const target=leave;setLeave(null);setDirty(false);if(target){if(target.mode!==mode)setMode(target.mode);else if(target.id)void select(target.id);}}}>Discard edits and leave</Button></DialogContent></Dialog>

 <Dialog open={Boolean(evidence)} onOpenChange={v=>{if(!v)setEvidence(null);}}><DialogContent className="evidence-dialog"><DialogHeader><DialogTitle>Conversation source</DialogTitle><DialogDescription>{evidence?.original_transcript?"Urdu-script rendering of the saved transcript. The original is preserved below.":"Read-only transcript. Your clinical edits do not overwrite it."}</DialogDescription></DialogHeader><div className="evidence-scroll"><p className="source-transcript" dir={evidence?.display_language==='ur'?'rtl':'auto'} lang={evidence?.display_language||undefined}>{evidence?.transcript||'Transcript not available'}</p>{evidence?.original_transcript&&<details><summary>Original transcript (as returned)</summary><p dir="auto">{evidence.original_transcript}</p></details>}<details><summary>Original structured JSON</summary><pre>{JSON.stringify(evidence?.original,null,2)}</pre></details></div></DialogContent></Dialog>

 <Dialog open={metrics} onOpenChange={setMetrics}><DialogContent className="evidence-dialog"><DialogHeader><DialogTitle>Timing & API usage</DialogTitle><DialogDescription>Dollar amounts are estimates; reconcile against OpenAI Costs for reported billing.</DialogDescription></DialogHeader><div className="evidence-scroll"><p>Combined API call time: {c?.metrics?.elapsed_seconds.toFixed(1)} seconds</p><p>Estimated cost: {c?.metrics?.estimated_cost_usd==null?'Unavailable':'$'+c.metrics.estimated_cost_usd.toFixed(5)}</p>{c?.metrics?.calls.map((m,i)=><section key={i}><h3>{m.stage}</h3><p>{m.returned_model||m.requested_model} · {m.elapsed_seconds}s</p><p>Service tier: {m.service_tier||'Not returned'}</p><p>Request ID: {m.request_id||'Not returned'}</p><pre>{JSON.stringify(m.usage,null,2)}</pre></section>)}</div></DialogContent></Dialog></div>

 {c?.state==='reviewed'&&!dirty&&<PrintReport consultation={c}/>}</>;

}

