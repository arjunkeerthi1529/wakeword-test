"""Compare Whisper models on accuracy AND speed, using the same recording.

    python scripts/stt_bench.py                    # read the printed script aloud when prompted
    python scripts/stt_bench.py --wav logs/stt_bench.wav     # re-run on the saved recording
    python scripts/stt_bench.py --device 3 --threads 4 --models tiny.en,base.en,small.en --beams 1,5

For best results play a recorded scam-call script through a phone speaker next
to the Pi, exactly as in real use, instead of reading it. The recording is
saved to logs/stt_bench.wav, so every model/setting is scored on identical audio.

Columns:
  chunk_s  decode time for the first 5 s (what one live chunk costs)
  full_s   decode time for the whole recording
  WER      word error rate vs the script (lower is better)
  keywords how many of the scam-critical words were heard
"""
import argparse
import re
import time
import wave
from pathlib import Path

import numpy as np

SR = 16_000
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WAV = ROOT / "logs" / "stt_bench.wav"

SCRIPT = (
    "I am calling from the cyber police department. You are linked to a criminal case. "
    "Do not tell anyone. Please read me the OTP that was sent to your phone. "
    "Install AnyDesk now and transfer the money to the safe account."
)
KEYWORDS = ["police", "criminal", "tell anyone", "otp", "anydesk", "transfer", "safe account"]
PROMPT = ("A phone call about a bank account: OTP, one time password, verification code, "
          "UPI PIN, CVV, KYC, Aadhaar, PAN card, debit card, credit card, AnyDesk, "
          "TeamViewer, refund, transfer, customer care.")


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def wer(ref: str, hyp: str) -> float:
    r, h = norm(ref).split(), norm(hyp).split()
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1] / max(1, len(r))


def keywords_found(hyp: str) -> str:
    n = norm(hyp)
    hits = [k for k in KEYWORDS if k in n]
    return f"{len(hits)}/{len(KEYWORDS)}"


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path} must be 16 kHz, mono, 16-bit")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def save_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def record(seconds: int, device) -> np.ndarray:
    import sounddevice as sd
    print("\nSCRIPT TO READ (or play through the phone speaker):\n")
    print("  " + SCRIPT + "\n")
    input("Press Enter, then start reading immediately… ")
    print(f"Recording {seconds} s…")
    audio = sd.rec(seconds * SR, samplerate=SR, channels=1, dtype="float32", device=device)
    sd.wait()
    return audio.flatten()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=None)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--seconds", type=int, default=22)
    ap.add_argument("--wav", default=None, help="reuse a saved recording instead of recording")
    ap.add_argument("--models", default="tiny.en,base.en,small.en")
    ap.add_argument("--beams", default="1,5")
    ap.add_argument("--no-prompt", action="store_true", help="disable the scam vocabulary prompt")
    args = ap.parse_args()

    if args.wav:
        audio = load_wav(Path(args.wav))
    else:
        audio = record(args.seconds, args.device)
        save_wav(DEFAULT_WAV, audio)
        print(f"Saved to {DEFAULT_WAV} (re-run with --wav to reuse)")
    peak = float(np.abs(audio).max())
    print(f"\n{len(audio) / SR:.1f} s of audio, peak level {peak:.3f}"
          + ("  (very quiet — move closer / raise mic gain)" if peak < 0.05 else ""))
    audio = audio / peak * 0.9 if peak > 0 else audio

    from faster_whisper import WhisperModel
    noise = (np.random.default_rng(0).standard_normal(SR * 2) * 0.05).astype(np.float32)
    prompt = None if args.no_prompt else PROMPT
    first5 = audio[: 5 * SR]

    def run(model, clip, beam):
        t0 = time.monotonic()
        segs, _ = model.transcribe(clip, language="en", beam_size=beam, vad_filter=True,
                                   condition_on_previous_text=False, initial_prompt=prompt,
                                   temperature=(0.0, 0.2, 0.4), without_timestamps=True)
        text = " ".join(s.text.strip() for s in segs).strip()
        return time.monotonic() - t0, text

    print(f"\n{'model':9} {'beam':>4} {'chunk_s':>8} {'full_s':>7} {'WER':>6} {'keywords':>9}  transcript")
    for name in args.models.split(","):
        model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=args.threads)
        list(model.transcribe(noise, language="en", beam_size=1, vad_filter=False)[0])   # warm-up
        for beam in [int(b) for b in args.beams.split(",")]:
            chunk_s, _ = run(model, first5, beam)
            full_s, text = run(model, audio, beam)
            print(f"{name:9} {beam:>4} {chunk_s:>8.1f} {full_s:>7.1f} {wer(SCRIPT, text):>6.0%} "
                  f"{keywords_found(text):>9}  {text}")
        del model
    print("\nPick the most accurate row whose chunk_s is small enough for live use "
          "(the service batches speech while STT is busy, so chunk_s ≈ the delay per update).")


if __name__ == "__main__":
    main()
