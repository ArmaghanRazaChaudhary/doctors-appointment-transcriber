from .config import CLINICAL_MODEL, MAX_OUTPUT_TOKENS, TRANSCRIPTION_MODEL, REASONING_EFFORT
from .prompts import CLINICAL_PROMPT, TRANSCRIPTION_PROMPT
from .schemas import ClinicalNote


def transcribe(client, audio_path, accounting):
    with audio_path.open("rb") as audio:
        result = accounting.call("transcription", TRANSCRIPTION_MODEL, lambda: client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL, file=audio, response_format="json",
            prompt=TRANSCRIPTION_PROMPT,
        ))
    if not result.text or not result.text.strip():
        raise ValueError("Transcription was empty; clinical extraction was not attempted.")
    return result


def extract_note(client, transcript, accounting):
    options = {"reasoning": {"effort": REASONING_EFFORT}} if REASONING_EFFORT else {}
    result = accounting.call("clinical_structuring", CLINICAL_MODEL, lambda: client.responses.parse(
        model=CLINICAL_MODEL, store=False, max_output_tokens=MAX_OUTPUT_TOKENS,
        input=[{"role": "system", "content": CLINICAL_PROMPT},
               {"role": "user", "content": transcript}],
        text_format=ClinicalNote, **options,
    ))
    if result.status != "completed" or result.output_parsed is None:
        raise ValueError("Clinical response was incomplete or refused; no completed note was saved.")
    return result
