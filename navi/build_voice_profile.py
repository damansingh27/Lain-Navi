from resemblyzer import VoiceEncoder, preprocess_wav
from pathlib import Path
import numpy as np

encoder = VoiceEncoder()

print("Building voice profile from assets/my_voice.wav...")
wav = preprocess_wav(Path("assets/my_voice.wav"))
embed = encoder.embed_utterance(wav)

np.save("assets/voice_profile.npy", embed)
print("Voice profile saved to assets/voice_profile.npy")