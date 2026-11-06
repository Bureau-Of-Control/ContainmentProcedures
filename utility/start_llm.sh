#!/usr/bin/env bash
# start_llm.sh — launch Ollama + ollama_proxy.py together.
# Ctrl-C stops both. Logs: /tmp/ollama.log, /tmp/ollama_proxy.log

set -euo pipefail

cd "$(dirname "$0")"

export OLLAMA_CONTEXT_LENGTH=78000
export OLLAMA_KV_CACHE_TYPE=q8_0
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_KEEP_ALIVE=24h
export OLLAMA_MAX_QUEUE=512

# Reasoning coalescing defaults are now built into ollama_proxy.py (INTERVAL=5, CHARS=1000).
# Override here only if you want different values than the proxy defaults:
# export PROXY_REASONING_FLUSH_INTERVAL=30   # fewer, larger cards; no loop monitoring needed
# export PROXY_REASONING_FLUSH_CHARS=999999 # effectively disable size-flush

# Start Ollama.
ollama serve > /tmp/ollama.log 2>&1 &

OLLAMA_PID=$!
PROXY_PID=""

cleanup() {
    if [ -n "$PROXY_PID" ]; then
        kill "$PROXY_PID" 2>/dev/null || true
    fi

    kill "$OLLAMA_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

# Wait for Ollama to become available.
READY=0

for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:11434/api/version \
        > /dev/null; then
        READY=1
        break
    fi

    sleep 1
done

if [ "$READY" -ne 1 ]; then
    echo "Ollama failed to start. Check /tmp/ollama.log"
    exit 1
fi

# Pre-warm the model.
ollama run qwen3-coder ""

# Optional shared-secret auth for the proxy. Leave PROXY_PASSWORD empty for an unauthenticated local-only setup (the default). Set it if you expose port 8050/8080 on a LAN/tunnel — then VS Code BYOM must use this value as its API key, and the web UI (/_log, /_status) asks for username PROXY_AUTH_USER + this password via HTTP Basic auth. See utility/ollama_proxy.md → "Optional authentication".
PROXY_PASSWORD=""          # e.g. 's3cr3t-pw' to enable auth; empty = disabled
PROXY_AUTH_USER="proxy"    # Basic-auth username shown in the browser pop-up

AUTH_ARGS=()
if [ -n "$PROXY_PASSWORD" ]; then
    AUTH_ARGS=(--password "$PROXY_PASSWORD" --auth-user "$PROXY_AUTH_USER")
fi

# Start the proxy. The proxy tees its own stdout/stderr into --log-file (so /_log web UI works). We redirect shell-level stdout/stderr to /dev/null so nothing spills to the terminal; all logging goes through the internal tee to the real file.
python3 -u ollama_proxy.py \
    --model qwen3-coder \
    --port 8050 \
    --filter-windows-tools \
    "${AUTH_ARGS[@]}" \
    --log-file /tmp/ollama_proxy.log \
    > /dev/null 2>&1 &

PROXY_PID=$!

echo "Ollama and proxy are running."
echo "Proxy: http://127.0.0.1:8050"
[ -n "$PROXY_PASSWORD" ] && echo "Auth: ENABLED — use '$PROXY_AUTH_USER' + your PROXY_PASSWORD as the BYOM API key / Basic-auth password." || echo "Auth: disabled (no PROXY_PASSWORD set)."
echo "Press Ctrl-C to stop."

wait "$OLLAMA_PID"