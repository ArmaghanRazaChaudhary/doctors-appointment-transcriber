"""Application orchestration for one recorded consultation."""
import datetime
import json
import os
import sys

from .audio import record_consultation
from .config import AUDIO_FILE_OVERRIDE, QWEN_OLLAMA_MODEL, RECORDINGS_DIR, WHISPER_MODEL_ID
from .extraction import enforce_authoritative_medication_identities, generate_clinical_note, release_qwen_model
from .medicines import annotate_medications
from .presentation import display_clinical_note, save_note
from .transcription import transcribe_audio
from .validation import validate_clinical_data, validate_transcript_evidence

def main():
    print()
    print("=" * 60)
    print("       CLINICAL VOICE-NOTE SYSTEM")
    print("=" * 60)
    print()
    print('Whisper:')
    print(f'  {WHISPER_MODEL_ID}')
    print()
    print('Qwen:')
    print(f'  {QWEN_OLLAMA_MODEL}')

    if AUDIO_FILE_OVERRIDE:
        audio_path = AUDIO_FILE_OVERRIDE
        if not os.path.isfile(audio_path):
            print(f"\nConfigured audio file does not exist:\n{audio_path}")
            sys.exit(1)
        print(f"\nUsing existing recording:\n{audio_path}")
    else:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_path = os.path.join(RECORDINGS_DIR, f"consult_{timestamp}.wav")
        try:
            record_consultation(audio_path)
        except KeyboardInterrupt:
            print("\nRecording cancelled.")
            sys.exit(0)
        except Exception as exc:
            print(f"\nRecording error:\n{exc}")
            sys.exit(1)

    # WHISPER
    try:
        transcript = transcribe_audio(audio_path)
    except Exception as e:
        print()
        print(f'Whisper error:\n{e}')
        sys.exit(1)
    if not transcript.strip():
        print()
        print('ERROR: Whisper returned an empty transcript.')
        sys.exit(1)

    # MEDICINE MATCHING
    annotated_transcript = annotate_medications(transcript)
    if annotated_transcript != transcript:
        print()
        print('=' * 60)
        print('MEDICATION-ANNOTATED TRANSCRIPT')
        print('=' * 60)
        print()
        print(annotated_transcript)

    # QWEN
    try:
        structured_data = generate_clinical_note(annotated_transcript)
    except Exception as exc:
        print()
        print(f'Qwen error:\n{exc}')
        sys.exit(1)
    finally:
        release_qwen_model()

    # HARD MEDICATION IDENTITY SAFETY GATE
    structured_data = enforce_authoritative_medication_identities(
        structured_data,
        annotated_transcript
    )
    print()
    print("=" * 60)
    print("RAW QWEN JSON")
    print("=" * 60)
    print(json.dumps(structured_data, ensure_ascii=False, indent=2))

    # VALIDATION
    structured_data = validate_transcript_evidence(structured_data, transcript)
    structured_data = validate_clinical_data(structured_data)

    # DISPLAY
    display_clinical_note(structured_data)

    # SAVE
    output_path = save_note(transcript, annotated_transcript, structured_data, audio_path)
    print()
    print("=" * 60)
    print("SAVED")
    print("=" * 60)
    print()
    print(f'Clinical note saved to:')
    print(output_path)
    print()
    print('Status: PENDING DOCTOR REVIEW')
    print()
    print("=" * 60)
