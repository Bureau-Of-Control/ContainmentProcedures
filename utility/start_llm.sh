#!/usr/bin/env bash
# start_llm.sh — launch Ollama + ollama_proxy.py together.
# Ctrl-C stops both. Logs: /tmp/ollama.log, /tmp/ollama_proxy.log
set -euo pipefail
cd "$(dirname "$0")"

export OLLAMA_CONTEXT_LENGTH=82000
export OLLAMA_KV_CACHE_TYPE=q8_0

# 1) Start Ollama in the background
nohup ollama serve > /tmp/ollama.log 2>&1 &
OLLAMA_PID=$!
PROXY_PID=""

# Stop everything on Ctrl-C / script exit
cleanup() { [ -n "$PROXY_PID" ] && kill "$PROXY_PID" 2>/dev/null; kill "$OLLAMA_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# 2) Wait until Ollama answers, then pre-warm the model
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:11434/api/version > /dev/null; then break; fi
    sleep 1
done
ollama run qwen3-coder ""

# 3) Start the proxy in front of Ollama (internal port 8050, bound to host 8080)
python3 -u ollama_proxy.py --model qwen3-coder --port 8050 --filter-windows-tools > /tmp/ollama_proxy.log 2>&1 &
PROXY_PID=$!

echo "Ollama (pid $OLLAMA_PID) + proxy (pid $PROXY_PID) running. Ctrl-C to stop both."
wait
