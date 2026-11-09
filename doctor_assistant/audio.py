"""Microphone capture, calibration, resampling, and WAV output."""
import threading
import wave

import numpy as np
import sounddevice as sd

from .config import CHANNELS, SAMPLE_RATE

def _get_native_samplerate():
    """
    Get the microphone's native sample rate.

    Windows microphones commonly operate at 44100 or 48000 Hz.
    We record at the native rate and resample to 16 kHz afterward.
    """
    try:
        device_info = sd.query_devices(kind="input")
        rate = int(device_info["default_samplerate"])
        return rate
    except Exception:
        return 44100


def _record_until_enter(samplerate):
    """
    Record continuously until the user presses ENTER.

    This avoids requiring the user to know the consultation duration
    beforehand.
    """
    frames = []
    stop_event = threading.Event()

    def callback(indata, frame_count, time_info, status):
        if status:
            print(f"  (stream status: {status})")
        frames.append(indata.copy())
    print()
    print("=" * 60)
    print("RECORDING")
    print("=" * 60)
    print()
    print("Speak normally.")
    print("Press ENTER when the consultation is finished.")
    print()
    with sd.InputStream(
        samplerate=samplerate,
        channels=CHANNELS,
        dtype="float32",
        callback=callback,
    ):
        input()
    if not frames:
        raise RuntimeError('No audio captured. Check your microphone/input device.')
    audio = np.concatenate(frames, axis=0)
    return audio


def _resample_to_16k(audio, orig_rate):
    """
    Resample microphone audio to 16 kHz for Whisper.
    """
    if orig_rate == SAMPLE_RATE:
        return audio
    from scipy.signal import resample
    n_samples = int(len(audio) * SAMPLE_RATE / orig_rate)
    return resample(audio, n_samples)


def calibrate_ambient_noise(seconds=1.5):
    """
    Brief microphone check before the consultation starts.
    """
    print()
    print("=" * 60)
    print("MICROPHONE CHECK")
    print("=" * 60)
    print(f'Please stay quiet for {seconds} seconds while the microphone is checked...')
    native_rate = _get_native_samplerate()
    frames = []

    def callback(indata, frame_count, time_info, status):
        if status:
            print(f"  (stream status: {status})")
        frames.append(indata.copy())
    with sd.InputStream(
        samplerate=native_rate,
        channels=CHANNELS,
        dtype="float32",
        callback=callback,
    ):
        sd.sleep(int(seconds * 1000))
    if not frames:
        raise RuntimeError('Could not capture microphone audio.')
    ambient = np.concatenate(frames, axis=0)
    rms = float(np.sqrt(np.mean(ambient ** 2)))
    if rms < 1e-5:
        print('WARNING: Microphone appears silent or disconnected.')
    elif rms > 0.05:
        print(f'WARNING: High ambient noise detected (RMS={rms:.5f}).')
    else:
        print(f'Microphone OK (ambient RMS={rms:.5f}, native rate={native_rate} Hz)')
    return native_rate


def record_consultation(out_path):
    """
    Record an entire consultation until ENTER is pressed.
    """
    native_rate = calibrate_ambient_noise()
    print()
    print("=" * 60)
    print("READY")
    print("=" * 60)
    print()
    print("Press ENTER to START the consultation recording.")
    input()
    print()
    print("Starting recording...")
    audio = _record_until_enter(native_rate)
    print()
    print("Recording stopped.")

    # Quality checks
    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))
    if rms < 1e-4:
        print('WARNING: Recording is nearly silent.')
    if peak >= 0.99:
        print('WARNING: Possible clipping detected.')

    # Resample
    audio_16k = _resample_to_16k(audio, native_rate)

    # Convert to 16-bit PCM
    audio_int16 = np.clip(audio_16k, -1.0, 1.0)
    audio_int16 = (audio_int16 * 32767).astype(np.int16)

    # Save WAV
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_int16.tobytes())
    duration_seconds = len(audio) / native_rate
    print()
    print(f'Saved recording: {out_path}')
    print(f'Recording duration: {duration_seconds:.1f} seconds')
    return out_path
