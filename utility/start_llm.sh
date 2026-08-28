#!/usr/bin/env bash
# start_llm.sh — launch Ollama + ollama_proxy.py together.
# Ctrl-C stops both. Logs: /tmp/ollama.log, /tmp/ollama_proxy.log
set -euo pipefail
cd "$(dirname "$0")"

# Ollama server environment (see ollama_proxy.md "Suggested Ollama environment").
# NUM_PARALLEL=1 is what makes the proxy's queue-wait / patient-retry logic
# meaningful: a single slot means every request either runs or queues.
export OLLAMA_CONTEXT_LENGTH=82000
export OLLAMA_KV_CACHE_TYPE=q8_0
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_KEEP_ALIVE=24h
export OLLAMA_MAX_QUEUE=512

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

# 3) Start the proxy in front of Ollama (binds 0.0.0.0:8050; upstream is 127.0.0.1:11434).
#    The shell redirect is the single log writer; --log-file /dev/null disables
#    the internal tee so we don't double-write the same file (and no stray
#    ollama_proxy.log appears in this directory).
python3 -u ollama_proxy.py --model qwen3-coder --port 8050 \
    --filter-windows-tools --log-file /dev/null > /tmp/ollama_proxy.log 2>&1 &
PROXY_PID=$!

echo "Ollama (pid $OLLAMA_PID) + proxy (pid $PROXY_PID) running. Ctrl-C to stop both."
wait
