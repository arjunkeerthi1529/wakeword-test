"""Speak one sentence exactly the way the assistant does, and print every step.

    python scripts/tts_test.py
    python scripts/tts_test.py --text "Testing one two three"
    python scripts/tts_test.py --aplay plughw:3,0      # try a specific aplay device
    python scripts/tts_test.py --aplay auto

Shows which route is used (aplay or sounddevice), the exact device, and any error.
"""
import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_config
from src.tts.piper_engine import PiperEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="Hello. This is a speaker test. Can you hear me?")
    ap.add_argument("--aplay", default=None, help="aplay device, e.g. plughw:3,0 or auto (overrides config)")
    ap.add_argument("--device", default=None, help="sounddevice output (overrides config)")
    args = ap.parse_args()

    cfg = get_config()
    aplay = args.aplay if args.aplay is not None else cfg.tts_aplay_device
    device = args.device if args.device is not None else cfg.tts_output_device

    print("\n--- `aplay -l` (playback cards) ---")
    try:
        print(subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5).stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"(could not run aplay: {exc})")

    print(f"\n--- config: tts_aplay_device={aplay!r}  tts_output_device={device!r} ---")
    tts = PiperEngine(cfg.piper_voice, output_device=device, aplay_device=aplay)
    t0 = time.monotonic()
    audio, rate = tts.synthesize(args.text)
    print(f"\nsynthesized {len(audio) / rate:.1f} s of audio at {rate} Hz in {time.monotonic() - t0:.1f} s "
          f"(peak level {abs(audio).max()} of 32767)")
    t0 = time.monotonic()
    print("playing now — listen…")
    tts.play(audio, rate)
    print(f"play() returned after {time.monotonic() - t0:.1f} s")
    print("\nHeard it?  yes -> note the settings above and put them in config/raspberrypi.yaml\n"
          "           no  -> check the ERROR lines above, then `aplay -D plughw:<card>,0 /usr/share/sounds/alsa/Front_Center.wav`")


if __name__ == "__main__":
    main()
