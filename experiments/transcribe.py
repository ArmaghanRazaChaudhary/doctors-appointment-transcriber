import sounddevice as sd
from scipy.io.wavfile import write
import torch
from transformers import pipeline

SAMPLE_RATE = 16000
SECONDS = 5

print("Recording for 5 seconds...")
print("Speak naturally in Pakistani Urdu / Urdu-English.")

audio = sd.rec(
int(SECONDS * SAMPLE_RATE),
samplerate=SAMPLE_RATE,
channels=1,
dtype="int16"
)

sd.wait()
write("recording.wav", SAMPLE_RATE, audio)

print("Recording finished. Loading model...")

pipe = pipeline(
"automatic-speech-recognition",
model="openai/whisper-large-v3",
torch_dtype=torch.float16,
device=0,
)

print("Transcribing...")

result = pipe(
"recording.wav",
generate_kwargs={
"language": "urdu",
"task": "transcribe",
},
)

print("\n================ TRANSCRIPTION ================\n")
print(result["text"])
print("\n================================================")
