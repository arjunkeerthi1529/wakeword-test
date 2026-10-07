#!/usr/bin/env bash
# Download Piper TTS voice model for Raspberry Pi.
# Run from the project root: bash scripts/download_models.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODELS_DIR="$PROJECT_DIR/models/piper"
mkdir -p "$MODELS_DIR"

BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low"
VOICE="en_US-lessac-low"

for ext in onnx onnx.json; do
    out="$MODELS_DIR/${VOICE}.${ext}"
    if [ -f "$out" ]; then
        echo "Already have: $out"
    else
        echo "Downloading: ${VOICE}.${ext}..."
        curl -fL --progress-bar -o "$out" "${BASE_URL}/${VOICE}.${ext}"
        echo "Saved: $out"
    fi
done

echo "Piper model ready at: $MODELS_DIR"
