"""Shared Urdu-English transcription settings for live and uploaded audio."""
from clinical_app.prompts import TRANSCRIPTION_PROMPT as BASE_PROMPT
LANGUAGES=['ur','en']
TRANSCRIPTION_PROMPT=BASE_PROMPT+'\nWrite Urdu speech in Urdu (Perso-Arabic) script, never Devanagari. Keep English speech and medical names in English.'
def transcribe(client,audio_path,accounting):
    import pipeline
    model=pipeline.SETTINGS['transcription_model']
    options={'extra_body':{'languages':LANGUAGES}} if model=='gpt-transcribe' else {'language':'ur'}
    with audio_path.open('rb') as audio:
        result=accounting.call('transcription',model,lambda:client.audio.transcriptions.create(model=model,file=audio,response_format='json',prompt=TRANSCRIPTION_PROMPT,**options))
    if not result.text or not result.text.strip():raise ValueError('Empty transcription')
    return result
