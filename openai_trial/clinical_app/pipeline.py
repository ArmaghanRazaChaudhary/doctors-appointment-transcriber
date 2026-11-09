import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import CLINICAL_MODEL, MAX_AUDIO_BYTES, OUTPUT_DIR, TRANSCRIPTION_MODEL, REASONING_EFFORT
from .storage import save_json
from .validation import check_evidence


def main():
    parser = argparse.ArgumentParser(description="OpenAI-only clinical note from one existing recording.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--audio", type=Path, help="Existing audio file; sends audio to OpenAI when run.")
    source.add_argument("--transcript", type=Path, help="Reuse a saved UTF-8 transcript; skips audio billing.")
    args = parser.parse_args()
    if REASONING_EFFORT:
        if CLINICAL_MODEL not in {"gpt-5.6-sol", "gpt-5.6", "gpt-5.5"}:
            parser.error("Reasoning override is configured only for gpt-5.6-sol, gpt-5.6 and gpt-5.5.")
        allowed = {"none", "low", "medium", "high", "xhigh"}
        if CLINICAL_MODEL != "gpt-5.5":
            allowed.add("max")
        if REASONING_EFFORT not in allowed:
            parser.error("Unsupported OPENAI_REASONING_EFFORT for the selected model.")
    path = (args.audio or args.transcript).expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        parser.error("Choose an existing, nonempty input file.")
    if args.audio:
        if path.suffix.lower() not in {".wav", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".webm"}:
            parser.error("Unsupported audio format.")
        if path.stat().st_size > MAX_AUDIO_BYTES:
            parser.error("Audio exceeds 25 MB; compress or split it before running.")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    key_file = Path(__file__).resolve().parents[1] / "api-key.txt"
    if not api_key and key_file.is_file():
        api_key = key_file.read_text(encoding="utf-8-sig").strip()
    if not api_key or api_key == "PASTE_YOUR_API_KEY_HERE":
        parser.error("Paste your API key into openai_trial/api-key.txt, or set OPENAI_API_KEY.")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from openai import OpenAI, APIError
    from .services import extract_note, transcribe
    from .accounting import CallLog

    run_dir = OUTPUT_DIR / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    accounting = CallLog(run_dir)
    metadata = {"status": "started", "source_file": str(path),
                "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "transcription_model": TRANSCRIPTION_MODEL if args.audio else None,
                "clinical_model": CLINICAL_MODEL, "requested_reasoning_effort": REASONING_EFFORT or None,
                "medication_identity_source": "model_proposed"}
    save_json(run_dir / "run.json", metadata)
    output = {"status": "failed", "output_dir": str(run_dir)}
    try:
        # Explicitly avoid automatic paid retries. Saved transcript can be reused on failure.
        with OpenAI(api_key=api_key, timeout=300.0, max_retries=0) as client:
            if args.audio:
                transcription = transcribe(client, path, accounting)
                transcript = transcription.text
                save_json(run_dir / "transcription.json", transcription.model_dump(mode="json"))
            else:
                transcript = path.read_text(encoding="utf-8-sig")
            if not transcript.strip():
                raise ValueError("Input transcript is empty.")
            (run_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
            result = extract_note(client, transcript, accounting)
        note = result.output_parsed
        warnings = check_evidence(note, transcript)
        metadata.update(status="pending_doctor_review", response_id=result.id,
                        usage=result.usage.model_dump(mode="json") if result.usage else None,
                        validation_warnings=warnings)
        output.update(schema_version="2", status="pending_doctor_review", note=note.model_dump(),
                      validation_warnings=warnings)
        save_json(run_dir / "note.json", {k: v for k, v in output.items() if k != "output_dir"})
        save_json(run_dir / "run.json", metadata)
        return 0
    except (APIError, ValueError, OSError) as exc:
        metadata.update(status="failed", error_type=type(exc).__name__)
        save_json(run_dir / "run.json", metadata)
        output.update(status="failed", error_type=type(exc).__name__)
        return 1
    finally:
        output["metrics"] = accounting.finish()
        print(json.dumps(output, ensure_ascii=False, indent=2))
