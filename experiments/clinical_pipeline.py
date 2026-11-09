"""
Clinical Voice-Note Pipeline
============================

Pipeline:
    Microphone -> Audio quality check -> Recording -> Whisper (Urdu
    fine-tune) -> Urdu/Urdu-English transcript -> Qwen (via Ollama) ->
    Structured clinical note -> saved to disk for doctor review
    (approval step is manual / for later, per your setup).

WHAT YOU NEED TO EDIT BEFORE RUNNING
-------------------------------------
Nothing, if `ollama list` shows your model exactly as
"hf.co/petruhonk/Qwen3.8-9B-Distill-uncensored-heretic-GGUF:Q8_0" and
Ollama is running. If the tag differs, update QWEN_OLLAMA_MODEL below
to match the NAME column from `ollama list` exactly.

INSTALL (one-time)
-------------------
    pip install transformers torch soundfile sounddevice numpy scipy requests

NOTE ON YOUR SETUP
-------------------
This script assumes:
  - Whisper is your local fine-tuned model `urdu-asr/whisper-large-v3-ur`,
    already sitting in your Hugging Face cache, loaded via `transformers`
    (NOT `faster-whisper`, since that needs a different, CTranslate2,
    format your model isn't in).
  - Qwen is managed by Ollama (confirmed via `ollama list`), called
    through Ollama's local REST API at http://localhost:11434.
    Ollama must be running -- it usually runs automatically as a
    background service on Windows after install; if not, run
    `ollama serve` in a separate terminal first.
"""

import os
import sys
import json
import wave
import datetime
import numpy as np
import sounddevice as sd

# ----------------------------------------------------------------------
# CONFIG - EDIT THESE
# ----------------------------------------------------------------------
WHISPER_MODEL_ID = "urdu-asr/whisper-large-v3-ur"  # already in your HF cache
WHISPER_LOCAL_ONLY = True   # True = never hit the network, fail loudly if cache is incomplete

QWEN_OLLAMA_MODEL = "hf.co/petruhonk/Qwen3.8-9B-Distill-uncensored-heretic-GGUF:Q8_0"
# ^ this must exactly match the NAME column from `ollama list`

SAMPLE_RATE = 16000                    # Whisper wants 16kHz
CHANNELS = 1
OUTPUT_DIR = "clinical_notes"          # where structured notes get saved

# ----------------------------------------------------------------------
# STEP 1-3: Audio quality / calibration + Recording
# ----------------------------------------------------------------------
def _get_native_samplerate():
    """Query the default input device's native sample rate. Forcing
    sd.rec()/sd.wait() to run at a rate the device doesn't natively
    support (e.g. 16000 Hz on a device that wants 44100/48000) is a
    known cause of sd.wait() hanging forever on Windows/WASAPI. So we
    record at whatever rate the device actually supports, then
    resample to 16kHz afterward for Whisper."""
    try:
        device_info = sd.query_devices(kind="input")
        rate = int(device_info["default_samplerate"])
        return rate
    except Exception:
        return 44100  # safe fallback


def _record_stream(duration_sec, samplerate):
    """Record using InputStream + sd.sleep() instead of sd.rec()/sd.wait().
    This avoids the hang some Windows setups hit with sd.wait()."""
    frames = []

    def callback(indata, frame_count, time_info, status):
        if status:
            print(f"  (stream status: {status})")
        frames.append(indata.copy())

    with sd.InputStream(samplerate=samplerate, channels=CHANNELS,
                         dtype="float32", callback=callback):
        sd.sleep(int(duration_sec * 1000))

    if not frames:
        raise RuntimeError("No audio captured. Check your mic/input device.")

    return np.concatenate(frames, axis=0)


def _resample_to_16k(audio, orig_rate):
    if orig_rate == SAMPLE_RATE:
        return audio
    from scipy.signal import resample
    n_samples = int(len(audio) * SAMPLE_RATE / orig_rate)
    return resample(audio, n_samples)


def calibrate_ambient_noise(seconds=1.5):
    """Sample ambient noise briefly so we can warn if the room is too loud
    or the mic looks dead before we waste a full recording."""
    print(f"Calibrating mic... please stay quiet for {seconds}s")
    native_rate = _get_native_samplerate()
    ambient = _record_stream(seconds, native_rate)
    rms = float(np.sqrt(np.mean(ambient ** 2)))
    if rms < 1e-5:
        print("  Warning: mic seems silent/disconnected. Check your input device.")
    elif rms > 0.05:
        print("  Warning: high ambient noise detected. Consider a quieter room.")
    else:
        print(f"  Mic OK (ambient RMS={rms:.5f}, native rate={native_rate}Hz)")
    return rms


def record_audio(duration_sec, out_path):
    """Record from the default microphone and save as a 16kHz mono WAV."""
    native_rate = _get_native_samplerate()
    print(f"Recording for {duration_sec}s at {native_rate}Hz... speak now.")
    audio = _record_stream(duration_sec, native_rate)
    print("Recording finished.")

    # quick post-recording quality check (on the raw, native-rate audio)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))
    if rms < 1e-4:
        print("  Warning: recording is nearly silent. Check mic volume.")
    if peak >= 0.99:
        print("  Warning: possible clipping detected (audio too loud).")

    audio_16k = _resample_to_16k(audio, native_rate)

    # convert float32 [-1,1] to int16 PCM for a standard WAV file
    audio_int16 = np.clip(audio_16k, -1.0, 1.0)
    audio_int16 = (audio_int16 * 32767).astype(np.int16)

    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_int16.tobytes())

    return out_path


# ----------------------------------------------------------------------
# STEP 4-5: Whisper large-v3 transcription -> Urdu/Urdu-English transcript
# ----------------------------------------------------------------------
def transcribe_audio(wav_path):
    import torch
    import soundfile as sf
    from transformers import pipeline

    device_index = 0 if torch.cuda.is_available() else -1
    print(f"Loading Whisper model '{WHISPER_MODEL_ID}' "
          f"({'GPU' if device_index == 0 else 'CPU'}, local_files_only={WHISPER_LOCAL_ONLY})...")

    if WHISPER_LOCAL_ONLY:
        # Force offline mode via env vars rather than passing local_files_only
        # to pipeline() directly -- on some transformers versions that kwarg
        # leaks through into model.generate() and crashes.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    try:
        asr = pipeline(
            "automatic-speech-recognition",
            model=WHISPER_MODEL_ID,
            device=device_index,
            chunk_length_s=30,
        )
    except OSError as e:
        raise RuntimeError(
            f"Could not load '{WHISPER_MODEL_ID}' from local cache. "
            "Your cache may be incomplete (missing files like model.safetensors "
            "or config.json). Either finish downloading it, or set "
            "WHISPER_LOCAL_ONLY = False at the top of this script to let it "
            "fetch any missing files."
        ) from e

    # read back the WAV we just wrote so we can pass a raw array (avoids
    # needing ffmpeg installed/on PATH)
    audio, sr = sf.read(wav_path)

    result = asr({"array": audio, "sampling_rate": sr})
    text = result["text"].strip()

    # Offload Whisper now that we have the transcript, so it doesn't hold
    # GPU memory while Qwen (via Ollama) runs next.
    print("Offloading Whisper model from memory...")
    del asr
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return text


# ----------------------------------------------------------------------
# STEP 5.5: Fuzzy-match drug names against known Pakistani medicine list
# ----------------------------------------------------------------------
MEDICINE_LIST_PATH = "medicines_pk.csv"
FUZZY_MATCH_THRESHOLD = 80   # 0-100 rapidfuzz score; lower = more lenient matching


def load_medicine_list(path=MEDICINE_LIST_PATH):
    import csv
    entries = []
    if not os.path.exists(path):
        print(f"  Note: medicine list not found at {path}, skipping drug-name matching.")
        return entries
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            spellings = [s.strip() for s in row["urdu_spellings"].split(";") if s.strip()]
            entries.append({
                "brand_name": row["brand_name"],
                "generic_name": row["generic_name"],
                "urdu_spellings": spellings,
            })
    return entries


def annotate_medications(transcript, medicine_list, threshold=FUZZY_MATCH_THRESHOLD):
    """Fuzzy-match each word in the transcript against known Pakistani drug
    name spellings and tag confident matches with [Brand/Generic], so the
    LLM is told what the drug actually is instead of guessing (and possibly
    substituting a similar-sounding but wrong medicine)."""
    if not medicine_list:
        return transcript

    from rapidfuzz import process, fuzz

    spelling_to_entry = {}
    for entry in medicine_list:
        for spelling in entry["urdu_spellings"]:
            spelling_to_entry[spelling] = entry
    all_spellings = list(spelling_to_entry.keys())

    annotated_words = []
    for word in transcript.split():
        match = process.extractOne(word, all_spellings, scorer=fuzz.ratio)
        if match and match[1] >= threshold:
            entry = spelling_to_entry[match[0]]
            annotated_words.append(f"{word}[{entry['brand_name']}/{entry['generic_name']}]")
        else:
            annotated_words.append(word)

    return " ".join(annotated_words)


# ----------------------------------------------------------------------
# STEP 6-7: Qwen -> Structured clinical note
# ----------------------------------------------------------------------
SYSTEM_PROMPT = """You are a clinical scribe assistant. You will receive a raw \
doctor-patient conversation transcript in Urdu, English, or a mix of both. \
Some words may be tagged like word[Brand/Generic] -- this means a drug-name \
matching system has confidently identified that word as a specific medicine. \
Trust these tags as ground truth for the medicine's identity; use the \
Brand/Generic name from the tag in your note, and do NOT substitute a \
different, similar-sounding drug name for it. For any medicine name NOT \
tagged this way, transcribe it as closely as possible to what was said \
rather than guessing or "correcting" it to a different drug.

Convert the transcript into a structured clinical note in English using this exact format:

Chief Complaint:
History of Present Illness:
Past Medical History:
Examination Findings:
Assessment:
Plan:

Rules:
- If information for a section is not mentioned in the transcript, write "Not mentioned".
- Do not invent any clinical facts that are not in the transcript.
- Keep the language concise and professional.
- Output ONLY the structured note, nothing else.
"""


def generate_clinical_note(transcript):
    import requests

    print(f"Generating structured note via Ollama ({QWEN_OLLAMA_MODEL})...")
    try:
        resp = requests.post(
            "http://localhost:11434/api/chat",
            json={
                "model": QWEN_OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                "stream": False,
                "think": False,      # skip the chain-of-thought trace -- this is
                                      # most of what was making generation feel slow
                "keep_alive": 0,     # unload Qwen from VRAM right after this call
                "options": {"temperature": 0.2},
            },
            timeout=300,
        )
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            "Could not reach Ollama at localhost:11434. Make sure Ollama is "
            "running (it usually runs as a background service on Windows; "
            "if not, run `ollama serve` in a separate terminal)."
        ) from e

    if not resp.ok:
        raise RuntimeError(
            f"Ollama returned {resp.status_code} for model "
            f"'{QWEN_OLLAMA_MODEL}':\n{resp.text}"
        )

    note = resp.json()["message"]["content"].strip()

    # This model exposes its chain-of-thought in <think>...</think> tags --
    # strip that out so it never ends up in an actual clinical note.
    import re
    note = re.sub(r"<think>.*?</think>", "", note, flags=re.DOTALL).strip()

    return note


# ----------------------------------------------------------------------
# STEP 8: Save for doctor review (approval step comes later)
# ----------------------------------------------------------------------
def save_note(transcript, annotated_transcript, structured_note, audio_path):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    record = {
        "timestamp": timestamp,
        "audio_file": audio_path,
        "raw_transcript": transcript,
        "medication_annotated_transcript": annotated_transcript,
        "structured_note": structured_note,
        "status": "pending_review",   # doctor will flip this to "approved" later
    }
    out_path = os.path.join(OUTPUT_DIR, f"note_{timestamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    print(f"\nSaved note for review: {out_path}")
    return out_path


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("duration", nargs="?", type=int, default=30,
                         help="Seconds to record (ignored if --file is given)")
    parser.add_argument("--file", type=str, default=None,
                         help="Path to an existing WAV file to use instead of recording")
    args = parser.parse_args()

    if args.file:
        audio_path = args.file
        if not os.path.exists(audio_path):
            print(f"File not found: {audio_path}")
            sys.exit(1)
        print(f"Using existing recording: {audio_path}")
    else:
        os.makedirs("recordings", exist_ok=True)
        audio_path = os.path.join("recordings", f"consult_{datetime.datetime.now():%Y%m%d_%H%M%S}.wav")
        calibrate_ambient_noise()
        record_audio(args.duration, audio_path)

    transcript = transcribe_audio(audio_path)
    print("\n--- TRANSCRIPT ---")
    print(transcript)

    medicine_list = load_medicine_list()
    annotated_transcript = annotate_medications(transcript, medicine_list)
    if annotated_transcript != transcript:
        print("\n--- MEDICATION-ANNOTATED TRANSCRIPT ---")
        print(annotated_transcript)

    structured_note = generate_clinical_note(annotated_transcript)
    print("\n--- STRUCTURED CLINICAL NOTE ---")
    print(structured_note)

    save_note(transcript, annotated_transcript, structured_note, audio_path)


if __name__ == "__main__":
    main()