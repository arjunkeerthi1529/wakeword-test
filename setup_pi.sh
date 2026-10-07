#!/usr/bin/env bash
# setup_pi.sh — One-shot Raspberry Pi 4 setup for pi-voice-assistant.
# Run as the pi user from the project root. Requires network access during setup.
set -euo pipefail

PI_HOME="$HOME"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$PI_HOME/pi-va-env"

echo "=== [1/8] System packages ==="
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip python3-venv python3-dev \
    cmake build-essential \
    libopenblas-dev \
    portaudio19-dev \
    libasound2-dev \
    git curl

# libgpiod was renamed between Bullseye (libgpiod2) and Bookworm (libgpiod3)
if apt-cache show libgpiod2 &>/dev/null; then
    sudo apt-get install -y libgpiod2
elif apt-cache show libgpiod3 &>/dev/null; then
    sudo apt-get install -y libgpiod3
else
    sudo apt-get install -y libgpiod-dev
fi

echo "=== [2/8] Python virtual environment ==="
python3 -m venv "$VENV"
# shellcheck source=/dev/null
source "$VENV/bin/activate"
pip install --upgrade pip

echo "=== [3/8] Python packages ==="
pip install -r "$PROJECT_DIR/requirements.txt"
pip install -r "$PROJECT_DIR/requirements-pi.txt"

echo "=== [4/8] Build whisper.cpp ==="
if [ ! -d "$PI_HOME/whisper.cpp" ]; then
    git clone https://github.com/ggerganov/whisper.cpp "$PI_HOME/whisper.cpp"
fi
cd "$PI_HOME/whisper.cpp"
git pull --ff-only
cmake -B build -DWHISPER_OPENBLAS=ON
cmake --build build --target whisper-cli -j4
echo "whisper-cli built at: $PI_HOME/whisper.cpp/build/bin/whisper-cli"

echo "=== [5/8] Download whisper tiny.en-q5_1 model ==="
bash models/download-ggml-model.sh tiny.en-q5_1
# Copy into the project's models/ directory
cp -v "$PI_HOME/whisper.cpp/models/ggml-tiny.en-q5_1.bin" "$PROJECT_DIR/models/"

echo "=== [6/8] Install Ollama and pull llama3.2:1b ==="
if ! command -v ollama &>/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
# Start Ollama if not already running
if ! systemctl is-active --quiet ollama 2>/dev/null; then
    ollama serve &
    OLLAMA_PID=$!
    sleep 5
fi
ollama pull llama3.2:1b

echo "=== [7/8] Piper voice model ==="
cd "$PROJECT_DIR"
bash scripts/download_models.sh

echo "=== [8/8] openWakeWord — pre-download hey_jarvis tflite model ==="
# Force model download now so it's cached before the demo
source "$VENV/bin/activate"
python3 -c "
from openwakeword.model import Model
print('Downloading openWakeWord hey_jarvis tflite model...')
Model(wakeword_models=['hey_jarvis'], inference_framework='tflite')
print('Download complete.')
"

echo "=== GPIO permissions ==="
sudo usermod -aG gpio "$USER"
echo "NOTE: Log out and back in (or run 'newgrp gpio') for GPIO group to take effect."

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Setup complete!                                     ║"
echo "║                                                      ║"
echo "║  Activate venv:  source $VENV/bin/activate"
echo "║  Run assistant:  python -m src.main                  ║"
echo "║  View latency:   cat latency_log.csv                 ║"
echo "╚══════════════════════════════════════════════════════╝"
