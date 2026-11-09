"""Application configuration and filesystem locations."""
import os
from pathlib import Path

PROJECT_ROOT = Path(os.getenv("DOCTOR_ASSISTANT_ROOT", Path(__file__).resolve().parent.parent))
DB_PATH = str(PROJECT_ROOT / "drap_master.db")
WHISPER_MODEL_ID = os.getenv("WHISPER_MODEL", "urdu-asr/whisper-large-v3-ur")
WHISPER_LOCAL_ONLY = True
QWEN_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "hf.co/petruhonk/Qwen3.8-9B-Distill-uncensored-heretic-GGUF:Q8_0")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
SAMPLE_RATE = 16000
CHANNELS = 1
OUTPUT_DIR = str(PROJECT_ROOT / "clinical_notes")
RECORDINGS_DIR = str(PROJECT_ROOT / "recordings")
AUDIO_FILE_OVERRIDE = os.getenv("AUDIO_FILE", "")   # empty: record from the microphone
MEDICINE_LIST_PATH = str(PROJECT_ROOT / "medicines_pk.csv")
FUZZY_MATCH_THRESHOLD = 85.5
FUZZY_MATCH_MARGIN = 5.0
FUZZY_PREFILTER_THRESHOLD = 64.0
FUZZY_SHORTLIST_LIMIT = 12
MEDICINE_FUZZY_INDEX_CACHE = str(PROJECT_ROOT / "medicine_fuzzy_index_v2.pkl")


