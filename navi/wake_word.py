"""
Wake-word detection and optional speaker verification for NAVI.

- Uses OpenWakeWord with a bundled ONNX model (default: hey_jarvis).
- Streams microphone audio in fixed-size chunks; when score exceeds threshold,
  invokes the `on_wake` callback (typically starts a short VAD recording in navi.py).
- `verify_speaker()` compares Resemblyzer embeddings against `assets/voice_profile.npy`
  when that file exists; if the file is missing, verification is skipped (always True).
"""

import time

import numpy as np
import openwakeword
import pyaudio
from openwakeword.model import Model
from pathlib import Path
from resemblyzer import VoiceEncoder, preprocess_wav

# ── Wake model & audio I/O ─────────────────────────────────────
# Default model id resolves to OpenWakeWord's bundled ONNX weights.
WAKE_WORD_MODEL = "hey_jarvis"
SIMILARITY_THRESHOLD = 0.55
DETECTION_THRESHOLD = 0.5
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280

# Resemblyzer encoder (GPU if available inside library); profile is a single embedding vector.
encoder = VoiceEncoder()
_voice_profile_abs = str(Path(__file__).resolve().parent / "assets" / "voice_profile.npy")

# Optional: without this file, verify_speaker() becomes a no-op (always True).
voice_profile = None
try:
    voice_profile = np.load(_voice_profile_abs)
except Exception:
    pass


def verify_speaker(audio_path):
    """
    Return True if the WAV at `audio_path` matches the enrolled voice embedding.

    Cosine similarity is computed in embedding space; threshold is SIMILARITY_THRESHOLD.
    If no `voice_profile.npy` was loaded, returns True (verification disabled).
    """
    if voice_profile is None:
        return True
    try:
        wav_data = preprocess_wav(Path(audio_path))
        embed = encoder.embed_utterance(wav_data)
        similarity = float(
            np.dot(voice_profile, embed)
            / (np.linalg.norm(voice_profile) * np.linalg.norm(embed))
        )
        print(f"Speaker similarity: {similarity:.2f}")
        return similarity >= SIMILARITY_THRESHOLD
    except Exception:
        return False


class WakeWordListener:
    """
    Blocking microphone loop: reads CHUNK_SIZE frames, runs OpenWakeWord predict,
    fires `on_wake` once per detection, then drains buffered audio and sleeps briefly
    to avoid double-triggering on the same utterance.
    """

    def __init__(self, on_wake):
        self.on_wake = on_wake
        self.model = Model(
            wakeword_models=[WAKE_WORD_MODEL],
            inference_framework="onnx",
        )
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            rate=SAMPLE_RATE,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )

    def listen(self):
        """Infinite loop until process exit; not stoppable except via Ctrl+C / app quit."""
        print("Listening for wake word...")
        while True:
            audio = self.stream.read(CHUNK_SIZE, exception_on_overflow=False)
            audio_data = np.frombuffer(audio, dtype=np.int16)
            prediction = self.model.predict(audio_data)
            score = list(prediction.values())[0]
            if score > DETECTION_THRESHOLD:
                print(f"Wake word detected. Score: {score:.2f}")
                self.on_wake()

                self.model.reset()

                try:
                    while self.stream.get_read_available() > 0:
                        self.stream.read(
                            self.stream.get_read_available(),
                            exception_on_overflow=False,
                        )
                except Exception:
                    pass

                time.sleep(1.5)
                print("Listening for wake word...")

    def cleanup(self):
        self.stream.close()
        self.pa.terminate()
