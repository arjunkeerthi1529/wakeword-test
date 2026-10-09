"""Measure which Whisper model can keep up with live speech on this machine.

    python scripts/stt_bench.py            # records 5 s from the default mic
    python scripts/stt_bench.py --device 3

Speak a normal sentence when prompted. For each model it prints seconds taken,
real-time factor (rtf = decode seconds / audio seconds; must stay below 1 to
keep up live) and the transcript, so you can trade accuracy against speed.
"""
import argparse
import time

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

SR = 16_000
SECONDS = 5

ap = argparse.ArgumentParser()
ap.add_argument("--device", default=None)
ap.add_argument("--threads", type=int, default=4)
args = ap.parse_args()

print(f"Speak a sentence for {SECONDS} s (recording starts now)…")
audio = sd.rec(SECONDS * SR, samplerate=SR, channels=1, dtype="float32", device=args.device)
sd.wait()
audio = audio.flatten()
peak = float(np.abs(audio).max())
if peak > 0:
    audio = audio / peak * 0.9
print(f"Recorded. Peak level before normalising: {peak:.3f}\n")

noise = (np.random.default_rng(0).standard_normal(SR * 2) * 0.05).astype(np.float32)
print(f"{'model':10} {'threads':>7} {'seconds':>8} {'rtf':>6}  transcript")
for name in ("tiny.en", "base.en", "small.en"):
    model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=args.threads)
    list(model.transcribe(noise, language="en", beam_size=1, vad_filter=False)[0])   # warm-up
    t0 = time.monotonic()
    segs, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                               condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segs).strip()
    took = time.monotonic() - t0
    print(f"{name:10} {args.threads:>7} {took:>8.1f} {took / SECONDS:>6.2f}  {text}")
    del model
