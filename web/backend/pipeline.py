"""Adapter for the existing OpenAI pipeline; no API call occurs on import."""
import json
import os
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from store import ROOT, LOCK, directory, load, save, now, write_json

TRIAL = ROOT.parent / 'openai_trial'
sys.path.insert(0, str(TRIAL))
SETTINGS = {'transcription_model':'gpt-transcribe','clinical_model':'gpt-5.6-sol','reasoning_effort':'low'}
settings_path = ROOT / 'backend' / 'settings.json'
if settings_path.exists():
    SETTINGS.update(json.loads(settings_path.read_text(encoding='utf-8-sig')))
os.environ['OPENAI_TRANSCRIPTION_MODEL'] = SETTINGS['transcription_model']
os.environ['OPENAI_CLINICAL_MODEL'] = SETTINGS['clinical_model']
os.environ['OPENAI_REASONING_EFFORT'] = SETTINGS['reasoning_effort']
from clinical_app.schemas import ClinicalNote
from clinical_app.prompts import CLINICAL_PROMPT
from transcription import transcribe
from clinical_app.validation import check_evidence
from accounting import CallLog
from openai import OpenAI

class WebNote(ClinicalNote):
    ai_diagnosis_paragraph: str

POOL = ThreadPoolExecutor(max_workers=2)

def get_key():
    value = os.getenv('OPENAI_API_KEY', '').strip()
    if not value and (TRIAL / 'api-key.txt').exists():
        value = (TRIAL / 'api-key.txt').read_text(encoding='utf-8-sig').strip()
    return value if value and value != 'PASTE_YOUR_API_KEY_HERE' else ''

def set_state(cid, **fields):
    with LOCK:
        c = load(cid)
        c.update(fields, updated_at=now())
        save(c)

def process(cid, reuse_transcript=False):
    run_dir = directory(cid)
    attempt = run_dir / ('attempt_' + now().replace(':','').replace('.',''))
    attempt.mkdir()
    log = CallLog(attempt)
    try:
        with OpenAI(api_key=get_key(), timeout=300, max_retries=0) as client:
            if reuse_transcript:
                transcript = (run_dir / 'transcript.txt').read_text(encoding='utf-8')
            else:
                set_state(cid, state='transcribing')
                audio = run_dir / load(cid)['audio_file']
                response = transcribe(client, audio, log)
                transcript = response.text
                write_json(attempt / 'transcription.json', response.model_dump(mode='json'))
                (run_dir / 'transcript.txt').write_text(transcript, encoding='utf-8')
            if not transcript.strip():
                raise ValueError('Empty transcript')
            set_state(cid, state='structuring')
            prompt = CLINICAL_PROMPT + '\nFor ai_diagnosis_paragraph only, write one concise English paragraph (maximum 65 words) summarizing provisional differential considerations, important uncertainty and focused checks. No lists, treatment advice or definitive diagnosis. This paragraph is for the doctor only and is excluded from the printed report. This paragraph is the sole exception to the brief-phrases/no-prose rule. Do not put it in clinician_assessment.'
            options = {'reasoning':{'effort':SETTINGS['reasoning_effort']}} if SETTINGS['reasoning_effort'] else {}
            result = log.call('clinical_structuring', SETTINGS['clinical_model'], lambda: client.responses.parse(
                model=SETTINGS['clinical_model'], store=False, max_output_tokens=6000,
                input=[{'role':'system','content':prompt},{'role':'user','content':transcript}],
                text_format=WebNote, **options))
            if result.status != 'completed' or result.output_parsed is None:
                raise ValueError('Incomplete clinical output')
            note = result.output_parsed.model_dump()
            base = ClinicalNote.model_validate({k:v for k,v in note.items() if k != 'ai_diagnosis_paragraph'})
            output = dict(schema_version='2', status='pending_doctor_review', note=note, validation_warnings=check_evidence(base, transcript))
            if load(cid).get('live',{}).get('capture_interrupted'):output['validation_warnings'].append('Microphone capture was interrupted. Verify the saved portion with the doctor before review.')
            write_json(run_dir / 'original_note.json', output)
            set_state(cid, state='review_required', result=output, error=None)
    except Exception as exc:
        code = getattr(exc, 'status_code', None)
        messages = {401:'API key rejected. Check openai_trial/api-key.txt.',429:'OpenAI quota or rate limit reached. No retry was sent. Check your API billing.',400:'OpenAI rejected the request. Check model access, audio format and settings.'}
        message = messages.get(code, 'Processing failed (' + type(exc).__name__ + '). Saved audio and any transcript are retained.')
        set_state(cid, state='failed', error=message)
    finally:
        log.finish()
        set_state(cid, metrics=log.document())

def submit(cid, reuse=False):
    POOL.submit(process, cid, reuse)
