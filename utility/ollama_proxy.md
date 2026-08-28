# ollama_proxy.py — VS Code BYOM payload proxy for local Ollama

A single-file Python 3 proxy (stdlib only, no dependencies) that sits between
VS Code's Bring-Your-Own-Model (BYOM) client and a local Ollama server. It
rewrites every request so it always targets **one fixed model**, decodes
thinking-depth directives out of the BYOM entry ID, optionally strips
Windows-native tools from payloads, and — most importantly — makes long model
"thinking" phases robust against socket timeouts, client aborts, and Ollama's
single generation slot.

```
VS Code (BYOM)  ──►  ollama_proxy.py :8050  ──►  Ollama :11434  ──►  qwen3-coder
```

## Why it exists

- **One model, many entries.** VS Code BYOM sends the entry *id* as the request
  `model`. The proxy forces every request to a single real Ollama model, so you
  can register several BYOM entries (different ids/params) that all hit the same
  local model.
- **Thinking depth via the id.** Ollama's OpenAI-compat endpoint ignores a
  top-level `think` field but honours `reasoning_effort`. The proxy decodes it
  from the entry id (see below).
- **Long thinking used to kill requests.** With `OLLAMA_NUM_PARALLEL=1`, a long
  generation holds Ollama's only slot; non-streaming replies stay silent in the
  *header* phase while the model thinks, which tripped socket timeouts and
  starved queued requests. This proxy is built around fixing exactly that.

## Quick start

```bash
python3 ollama_proxy.py \
    --model qwen3-coder \
    --port 8050 \
    --filter-windows-tools \
    --log-file /tmp/ollama_proxy.log
```

`start_llm.sh` in this folder starts Ollama + the proxy together. Point your VS
Code BYOM entries at `http://127.0.0.1:8050/v1`.

## CLI options

| Flag | Default | Meaning |
|------|---------|---------|
| `--model` (required) | — | Real Ollama model id forced into every request. |
| `--port` | `8085` | Port the proxy listens on. |
| `--ollama-url` | `http://127.0.0.1:11434` | Backend Ollama base URL. |
| `--default-effort` | *(none)* | `reasoning_effort` injected when the id has no directive (`none\|low\|medium\|high\|max`). |
| `--filter-windows-tools` | off | Strip Windows-native tools from chat-completion payloads (Linux-only envs). |
| `--log-file` | `ollama_proxy.log` | Duplicate stdout+stderr to this file (pass `/dev/null` to disable). |

## Thinking directives (model-id encoding)

The BYOM entry id is parsed with a trailing-directive regex and mapped to
`reasoning_effort`:

| Entry id suffix | `reasoning_effort` sent to Ollama |
|-----------------|-----------------------------------|
| `-nothink` | `none` |
| `-think-none` | `none` |
| `-think-low` | `low` |
| `-think-medium` | `medium` |
| `-think-high` | `high` |
| `-think-max` | `max` |

Example: a BYOM entry with id `qwen3-coder-think-medium` → request rewritten to
`model=qwen3-coder`, `reasoning_effort=medium`. No directive + no
`--default-effort` → nothing injected.

## Windows tool filtering (`--filter-windows-tools`)

For `POST /v1/chat/completions` only, tools whose name or description matches a
word-bounded pattern (`powershell|pwsh|cmd(.exe)?|win32|windows`, case-insensitive)
are removed from the payload. If that empties the tool list, `tools` and any
`tool_choice` referencing a Windows tool are dropped too. Word boundaries mean
harmless words like `command` or `cmdline` are **not** matched.

## How long thinking is made safe

This is the core of the proxy. Three cooperating mechanisms:

1. **Force streaming upstream for non-streaming clients.** When a client asks
   for a single JSON body (`stream=false`) on `/v1/chat/completions`, the proxy
   rewrites it to `stream=true` toward Ollama. Ollama then sends HTTP headers
   *immediately* on acceptance, so the header phase is always fast; the long
   thinking silence moves into the streaming body phase. The SSE chunks are
   reassembled (`_consume_sse_as_nonstream`) into the single JSON body the
   client expects (content, tool_calls, finish_reason, usage all preserved).

2. **Background reader with no socket read timeout.** `_iter_upstream_lines`
   reads the upstream body in a daemon worker thread after clearing the socket
   timeout (`sock.settimeout(None)`). A long thinking silence is just "no data
   yet" — it never raises. This fixes the old hard-stall bug where a buffered
   reader that timed out mid-read got *poisoned* (next read raised
   `OSError: cannot read from timed out object`) and crashed handler threads.

3. **Client-liveness polling.** The main loop polls `_client_alive()` (a
   non-blocking `MSG_PEEK` on the client socket) every ~5s while waiting for
   data. If the client is gone, it stops consuming; the caller's `finally`
   closes the upstream response, which aborts Ollama's generation and **frees
   its slot** instead of holding it for a dead client.

### Queue contention (single slot)

With `OLLAMA_NUM_PARALLEL=1`, new requests queue behind a running generation.
The proxy handles this in `_forward_with_retry`:

- The upstream socket timeout is set to `MAX_QUEUE_WAIT + UPSTREAM_READ_TIMEOUT`
  so the **queue-wait budget governs**, not a premature 30s socket timeout.
- A worker thread opens the connection; the main loop joins with a
  `MAX_QUEUE_WAIT` deadline while polling client liveness (disconnected client →
  abort upstream, free slot).
- Transient upstream errors (`429`, `5xx`) are retried with backoff — Ollama
  returns these when the slot is busy or it aborts a queued request. Permanent
  `4xx` errors surface immediately.

### Kill-and-respawn (non-streaming)

If the reader worker dies *before any data arrived* (poisoned socket, Ollama
dropped us mid-queue), nothing has been sent to the client yet — so the proxy
re-issues the request fresh, bounded by `UPSTREAM_RETRY_ATTEMPTS`.

## Control & observability

| Feature | Detail |
|---------|--------|
| `POST /_abort` | Closes **all** in-flight upstream connections → Ollama aborts its generation(s) and frees the slot immediately. Returns how many were closed. |
| Graceful shutdown | On SIGTERM/SIGINT, actively closes every in-flight upstream (frees the slot) then shuts down the server. Second Ctrl-C kills hard. |
| Per-request log | Each request gets an 8-hex `rid`; logs show header/queue-wait time and total time, e.g. `[proxy] 31cc7ffe headers in 65.7s (status=200)` … `[proxy] 31cc7ffe done in 98.2s`. |
| Crash tracebacks | `sys.excepthook` + a tee'd stdout/stderr write full tracebacks to the log file, flushed immediately so they survive a crash. |
| GET passthrough | `GET` requests (e.g. `/v1/models`, `/api/tags`) pass through unchanged. |

## Environment variables (tunables)

All have sane defaults; set them in the environment before starting the proxy.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROXY_UPSTREAM_TIMEOUT` | `30` | Socket timeout (s) for connect + header phase. Body reads are unbounded (see above). |
| `PROXY_UPSTREAM_RETRIES` | `3` | Max upstream attempts (connection failures, transient 429/5xx, kill-and-respawn). |
| `PROXY_UPSTREAM_BACKOFF` | `1.0` | Base backoff (s) between retries; multiplied by attempt number. |
| `PROXY_MAX_QUEUE_WAIT` | `120` | Max seconds to wait for response headers while queued behind a running generation. `0` disables the limit. |
| `PROXY_UPSTREAM_RCVBUF` | `4194304` (4 MiB) | `SO_RCVBUF` on upstream sockets — absorbs Ollama's post-thinking burst without backpressure stalling reads. |

## Suggested Ollama environment

```bash
export OLLAMA_NUM_PARALLEL=1        # single generation slot (this proxy assumes it)
export OLLAMA_CONTEXT_LENGTH=82000
export OLLAMA_KEEP_ALIVE=24h
export OLLAMA_MAX_QUEUE=512
```

## Notes & limitations

- **Chunked request bodies are not supported** — the proxy returns `411` rather
  than forward an empty body. VS Code / Node always send `Content-Length` for
  JSON POSTs, so this is defensive only.
- Retries only happen **before any response byte is sent** to the client; once
  streaming starts a re-issue isn't possible (kill-and-respawn covers the
  non-streaming case where nothing was sent yet).
- The proxy binds `0.0.0.0` — fine for a local dev container, but don't expose
  it unauthenticated on an open network.
