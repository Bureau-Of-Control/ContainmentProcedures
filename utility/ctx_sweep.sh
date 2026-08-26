#!/usr/bin/env bash
# ctx_sweep.sh — sweep OLLAMA_CONTEXT_LENGTH values and log resulting free VRAM.
#
# HOW IT WORKS
#   For each context window:
#     1. Restart the ollama server with OLLAMA_CONTEXT_LENGTH=<ctx>
#     2. Load the model with a tiny prompt (forces KV cache pre-allocation)
#     3. Record free VRAM from nvidia-smi
#
# WARNING: restarting ollama will terminate any active chat session
#          (including the one you're using to talk to the model).
#          Run this from a separate terminal and expect the session to drop.
#
# ASSUMPTIONS
#   - ollama runs as a plain process (not systemd/docker). If it's managed
#     differently, replace the restart block with your service command.
#   - KV cache type stays at the default q8_0 (OLLAMA_KV_CACHE_TYPE).

set -uo pipefail

MODEL="qwen3.8:27b"
WINDOWS=(96000 80000 76000 72000 68000 64000 56000 48000 40000 32000 28000 24000 20000 16000 12000)
TARGET_FREE_MIB=2048   # stop early once free VRAM >= 2 GB
LOG="ctx_sweep_$(date +%Y%m%d_%H%M%S).log"
API="http://127.0.0.1:11434"

free_mib() {
  nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | tr -d ' '
}

wait_for_api() {
  for _ in $(seq 1 30); do
    curl -sf "$API/api/version" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "  !! ollama API did not come up in 30s" >&2
  return 1
}

load_model() {
  # Tiny non-streaming generate: loads weights + pre-allocates KV cache.
  curl -sf "$API/api/generate" -d "{
    \"model\": \"$MODEL\",
    \"prompt\": \"hi\",
    \"stream\": false,
    \"options\": { \"num_predict\": 1 }
  }" >/dev/null 2>&1
}

echo "context_tokens  free_MiB  free_GB  notes" | tee "$LOG"
echo "----------------------------------------" | tee -a "$LOG"

for ctx in "${WINDOWS[@]}"; do
  echo "=== context=$ctx ==="
  echo "  stopping ollama..."
  pkill -f "ollama serve" 2>/dev/null || pkill -x ollama 2>/dev/null || true
  sleep 3

  echo "  starting ollama with OLLAMA_CONTEXT_LENGTH=$ctx"
  OLLAMA_CONTEXT_LENGTH=$ctx nohup ollama serve >/tmp/ollama_serve.log 2>&1 &
  if ! wait_for_api; then
    echo "$ctx  -  -  FAILED_TO_START" | tee -a "$LOG"
    continue
  fi

  echo "  loading model + allocating KV cache..."
  if load_model; then
    sleep 2
    free=$(free_mib)
    gb=$(awk "BEGIN {printf \"%.2f\", $free/1024}")
    if [ "$free" -ge "$TARGET_FREE_MIB" ]; then
      printf "%-14s %-9s %-8s ok  <-- TARGET MET (>=2GB free)\n" "$ctx" "$free" "$gb" | tee -a "$LOG"
      echo "  target reached at context=$ctx, stopping early."
      LAST_CTX=$ctx
      break
    fi
    printf "%-14s %-9s %-8s ok\n" "$ctx" "$free" "$gb" | tee -a "$LOG"
    LAST_CTX=$ctx
  else
    echo "$ctx  -  -  LOAD_FAILED (see /tmp/ollama_serve.log)" | tee -a "$LOG"
    LAST_CTX=$ctx
  fi
done

echo "----------------------------------------" | tee -a "$LOG"
echo "Done. Model is currently loaded at context=${LAST_CTX:-none}."
echo "Log: $LOG"
echo "To restore a specific window: OLLAMA_CONTEXT_LENGTH=<N> ollama serve"
