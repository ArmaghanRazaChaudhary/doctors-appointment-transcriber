import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "outputs"
TRANSCRIPTION_MODEL = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-transcribe")
CLINICAL_MODEL = os.getenv("OPENAI_CLINICAL_MODEL", "gpt-4.1")
REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "").strip().lower()
MAX_AUDIO_BYTES = 25_000_000
MAX_OUTPUT_TOKENS = 6000
