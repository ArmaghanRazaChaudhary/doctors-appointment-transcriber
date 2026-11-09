"""Local Whisper transcription, including overlap-safe long audio handling."""
import os
import re
import multiprocessing

from .config import SAMPLE_RATE, WHISPER_LOCAL_ONLY, WHISPER_MODEL_ID


def _remove_repetition_loops(text):
    """Drop token sequences repeated four or more times by ASR during silence."""
    words = str(text or '').split()
    normalized = [re.sub(r'[^\w\u0600-\u06ff]', '', word.lower()) for word in words]
    cleaned = []
    index = 0
    while index < len(words):
        removed = False
        for width in range(1, min(6, (len(words) - index) // 4) + 1):
            phrase = normalized[index:index + width]
            if not all(phrase):
                continue
            repeats = 1
            while normalized[index + repeats * width:index + (repeats + 1) * width] == phrase:
                repeats += 1
            if repeats >= 4:
                index += repeats * width
                removed = True
                break
        if not removed:
            cleaned.append(words[index])
            index += 1
    return ' '.join(cleaned)

def _clean_whisper_segment(text):
    """Remove obvious non-speech runs produced during long silences."""
    text = _remove_repetition_loops(str(text or '').replace('\ufffd', ''))
    pieces = re.split(r'[۔.…]{4,}', text)
    kept = [piece.strip(' ۔.…') for piece in pieces if len(re.findall(r'[\w\u0600-\u06ff]+', piece)) >= 3]
    return re.sub(r'\s+', ' ', '۔ '.join(kept)).strip()

def _merge_transcript_parts(parts):
    """Join overlapping ASR windows without repeating their shared words."""
    merged = []
    for part in parts:
        current = _clean_whisper_segment(part).split()
        if not current:
            continue
        normalized_current = [re.sub(r'[^\w\u0600-\u06ff]', '', word.lower()) for word in current]
        normalized_merged = [re.sub(r'[^\w\u0600-\u06ff]', '', word.lower()) for word in merged]
        overlap = 0
        for size in range(min(24, len(merged), len(current)), 1, -1):
            if normalized_merged[-size:] == normalized_current[:size]:
                overlap = size
                break
        merged.extend(current[overlap:])
    return ' '.join(merged).strip()

def _transcribe_overlapping_audio(asr, audio, sample_rate):
    """Transcribe long recordings in overlapping windows below Whisper's 30-second limit."""
    window_seconds, overlap_seconds = 28, 4
    if len(audio) / sample_rate <= window_seconds:
        return _clean_whisper_segment(asr({'array': audio, 'sampling_rate': sample_rate})['text'])
    window = int(window_seconds * sample_rate)
    step = int((window_seconds - overlap_seconds) * sample_rate)
    parts = []
    start = 0
    while start < len(audio):
        end = min(start + window, len(audio))
        if len(audio) - end <= int(overlap_seconds * sample_rate):
            end = len(audio)
        if end - start < sample_rate:
            break
        print(f'[WHISPER] Segment {len(parts) + 1}: {start / sample_rate:.0f}-{end / sample_rate:.0f}s', flush=True)
        result = asr({'array': audio[start:end], 'sampling_rate': sample_rate})
        parts.append(result['text'])
        if end == len(audio):
            break
        start += step
    return _merge_transcript_parts(parts)

def _transcribe_audio_local(wav_path):
    import torch
    import soundfile as sf

    device_index = 0 if torch.cuda.is_available() else -1
    device_name = 'GPU' if device_index == 0 else 'CPU'
    print('\n' + '=' * 60 + '\nWHISPER\n' + '=' * 60)
    print(f'Loading Whisper model:\n{WHISPER_MODEL_ID}')
    print(f'Device: {device_name}')
    print(f'Local only: {WHISPER_LOCAL_ONLY}')
    if WHISPER_LOCAL_ONLY:
        os.environ.setdefault('HF_HUB_OFFLINE', '1')
        os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
    from transformers import pipeline
    try:
        asr = pipeline('automatic-speech-recognition', model=WHISPER_MODEL_ID, device=device_index)
    except OSError as exc:
        raise RuntimeError(
            f"\nCould not load Whisper model '{WHISPER_MODEL_ID}' from the local cache.\n\n"
            'Your Hugging Face cache may be incomplete.\n'
            'Either finish downloading the model or set:\n\nWHISPER_LOCAL_ONLY = False\n'
        ) from exc
    audio, sample_rate = sf.read(wav_path)
    print('\nTranscribing consultation...')
    text = _transcribe_overlapping_audio(asr, audio, sample_rate)
    print('\n--- RAW TRANSCRIPT ---')
    print(text)
    print('\nOffloading Whisper from memory...')
    del asr
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return text


def _transcription_worker(wav_path, connection):
    try:
        connection.send((True, _transcribe_audio_local(wav_path)))
    except Exception as exc:
        connection.send((False, f'{type(exc).__name__}: {exc}'))
    finally:
        connection.close()


def transcribe_audio(wav_path):
    """Run Whisper in an isolated process so its CUDA context fully exits."""
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_transcription_worker, args=(wav_path, sender))
    process.start()
    sender.close()
    try:
        if not receiver.poll(900):
            process.terminate()
            process.join()
            raise RuntimeError('Whisper timed out after 15 minutes.')
        success, payload = receiver.recv()
    finally:
        receiver.close()
    process.join()
    if process.exitcode != 0:
        raise RuntimeError(f'Whisper worker exited with code {process.exitcode}.')
    if not success:
        raise RuntimeError(payload)
    return payload
