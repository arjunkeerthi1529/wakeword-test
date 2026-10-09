"""Find out where sound comes out: list every output device and play a test tone on it.

    python scripts/audio_check.py              # list devices, then tone on each output (listen!)
    python scripts/audio_check.py --device 3   # tone on one device (index or name part)
    python scripts/audio_check.py --list       # just list

Put the index (or a name part like "Jabra") that you hear into
config/raspberrypi.yaml as tts_output_device, or run with TTS_OUTPUT_DEVICE=3.
"""
import argparse
import time

import numpy as np
import sounddevice as sd


def tone(seconds=1.2, rate=16000, hz=440.0):
    t = np.arange(int(seconds * rate)) / rate
    fade = np.minimum(1.0, np.minimum(t, seconds - t) * 20)      # no click at start/end
    return (np.sin(2 * np.pi * hz * t) * 0.3 * fade * 32767).astype(np.int16), rate


def play(index, label):
    info = sd.query_devices(index)
    channels = 2 if info["max_output_channels"] >= 2 else 1
    audio, rate = tone()
    if channels == 2:
        audio = np.column_stack([audio, audio])
    print(f"  -> playing a 440 Hz beep on [{index}] {label} ({channels} ch) ... ", end="", flush=True)
    try:
        sd.play(audio, samplerate=rate, device=index)
        sd.wait()
        print("done")
    except Exception as exc:
        print(f"FAILED: {exc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=None)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    devices = sd.query_devices()
    default_out = sd.default.device[1]
    print("\nOutput devices (sounddevice):")
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0:
            mark = "  <- DEFAULT output" if i == default_out else ""
            print(f"  [{i}] {d['name']}  ({d['max_output_channels']} ch, {d['default_samplerate']:.0f} Hz){mark}")
    print("\nInput devices:")
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            mark = "  <- DEFAULT input" if i == sd.default.device[0] else ""
            print(f"  [{i}] {d['name']}  ({d['max_input_channels']} ch){mark}")
    if args.list:
        return

    print("\nListen for a beep on your headset/speaker after each line.\n")
    if args.device is not None:
        idx = int(args.device) if str(args.device).isdigit() else args.device
        info = sd.query_devices(idx)
        play(info["index"] if "index" in info else idx, info["name"])
        return
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0:
            play(i, d["name"])
            time.sleep(0.4)
    print("\nWhich index did you hear? Set tts_output_device to it in config/raspberrypi.yaml.")


if __name__ == "__main__":
    main()
