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

Both `tool_choice` shapes are handled when all tools are removed: dict-form
(`{"type":"function","function":{"name":...}}`) is dropped if it names a Windows
tool, and list-form (`["powershell", "bash"]`) keeps only the non-Windows entries
(dropping the field entirely if none remain).

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

**Two distinct busy-slot behaviors observed from Ollama:**

1. *Hard reject* — Ollama answers an overlapping request with a header-phase
   `503`/`429`. Handled by the patient-retry loop (see below).
2. *Silent queue* — Ollama **accepts** the request and queues it behind the
   running generation, sending no headers at all until the slot frees. This is
   what happens for VS Code context-compaction requests arriving mid-generation.
   The `MAX_QUEUE_WAIT` deadline bounds this wait; if it fires, the proxy logs
   `[proxy] upstream did not respond within Ns — request was likely queued
   behind an in-flight generation (single slot)` and surfaces a 503.

Because case 2 can legitimately last longer than a short generation, the
default `MAX_QUEUE_WAIT` is **300s** (kept at/under undici's default 300s
headers timeout so the client gives up first if it wants to). The wait loop
polls client liveness every 0.5s and bails early when the client leaves, so a
generous deadline costs nothing in that case.

### Kill-and-respawn (non-streaming)

If the reader worker dies *before any data arrived* (poisoned socket, Ollama
dropped us mid-queue), nothing has been sent to the client yet — so the proxy
re-issues the request fresh, bounded by `UPSTREAM_RETRY_ATTEMPTS`.

### Auto-recovery from a wedged slot

With `OLLAMA_NUM_PARALLEL=1`, an abandoned generation can hold the only slot
so that every later request queues until `MAX_QUEUE_WAIT` and fails — even
after Ollama itself has recovered. The proxy tracks consecutive failed
upstream attempts (every failure path in `_forward_with_retry`: deadline
exceeded, permanent error, retries exhausted). Once the streak reaches
`RECOVERY_FAIL_THRESHOLD`, it proactively calls `abort_all_upstreams()` to free
any wedged slot — re-establishing a clean connection with Ollama. A successful
request resets the streak. Current streak is visible via `GET /_health`.

### Keep-alive pings during deep thinking (client idle-timeout protection)

During deep thinking or context compaction, Ollama can emit **no bytes for
minutes**. A client with an idle timeout (VS Code ~150s) then closes its side
of the connection; the proxy sees EOF, aborts the in-flight generation to free
the slot — and the answer is lost. This is *not* a proxy-side socket timeout
(body reads are already unbounded); it's the **client** giving up on us.

The fix: while the client stays alive but Ollama is silent, `_iter_upstream_lines`
yields a `ping` sentinel every `KEEPALIVE_INTERVAL` seconds and the streaming
loop writes an SSE comment line (`: keep-alive`). Comment lines are ignored by
conforming SSE parsers (so the stream stays well-formed) but they reset the
client's idle timer, keeping VS Code connected while Ollama works. The
non-streaming reassembler skips `ping` sentinels safely.

### Patient retry for header-phase "slot busy" 503s

Keep-alive pings only fire **after** response headers arrive (body phase). A
rarer failure is a hard `429`/`5xx` in the **header phase**: VS Code abandons a
stalled prior request, re-issues an overlapping one, and Ollama — still busy on
the single slot — rejects it with a 503. The fast-retry loop (bounded by
`UPSTREAM_RETRY_ATTEMPTS`) handles the quick cases; if those are exhausted but
the client is *still waiting*, `_forward_with_retry_inner` enters a **patient
retry**: it re-issues on a slow cadence (`BUSY_RETRY_INTERVAL`) until the slot
frees, bounded by a total budget (`BUSY_RETRY_BUDGET`).

Two safety properties:

- The budget stays well under the client's idle timeout (~150s for VS Code), so
  we never hold a live client past its patience.
- If the client leaves mid-wait, the proxy stops waiting **without** calling
  `abort_all_upstreams()` — the busy slot belongs to *another* connection's
  generation (possibly a live one) and must be left alone; it frees itself when
  that generation finishes.

## Control & observability

| Feature | Detail |
|---------|--------|
| `GET /_health` | Proxy liveness probe — does **not** touch Ollama, so it works even when the upstream is wedged. Returns JSON: `{"status":"ok","active_upstreams":N,"fail_streak":M,"recovery_threshold":K}`. Handy for monitoring or auto-restart scripts. |
| `POST /_abort` | Closes **all** in-flight upstream connections → Ollama aborts its generation(s) and frees the slot immediately. Returns how many were closed. |
| Graceful shutdown | On SIGTERM/SIGINT, actively closes every in-flight upstream (frees the slot) then shuts down the server. Second Ctrl-C kills hard. |
| Per-request log | Each request gets an 8-hex `rid`; logs show the request identity (path, requested→target model, stream flag, tool count), header/queue-wait time, and total time. Example: `[2026-09-04 10:15:02] [proxy] a3f1c9e2 /v1/chat/completions requested='qwen3-coder-think-high' -> target='qwen3-coder' stream=True tools=3` … `[2026-09-04 10:15:02] [proxy] a3f1c9e2 headers in 0.3s (status=200)` … `[2026-09-04 10:17:11] [proxy] a3f1c9e2 done in 128.5s`. Every log line is prefixed with a `YYYY-MM-DD HH:MM:SS` timestamp for easy correlation across interleaved requests. |
| Crash tracebacks | `sys.excepthook` + a tee'd stdout/stderr write full tracebacks to the log file, flushed immediately so they survive a crash. |
| GET passthrough | `GET` requests (e.g. `/v1/models`, `/api/tags`) pass through unchanged. |

## Environment variables (tunables)

All have sane defaults; set them in the environment before starting the proxy.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROXY_UPSTREAM_TIMEOUT` | `30` | Socket timeout (s) for connect + header phase. Body reads are unbounded (see above). |
| `PROXY_UPSTREAM_RETRIES` | `3` | Max upstream attempts (connection failures, transient 429/5xx, kill-and-respawn). |
| `PROXY_UPSTREAM_BACKOFF` | `1.0` | Base backoff (s) between retries; multiplied by attempt number. |
| `PROXY_MAX_QUEUE_WAIT` | `300` | Max seconds to wait for response headers while queued behind a running generation (Ollama silently queues overlapping requests instead of rejecting them). Keep at/under the client's headers timeout (undici default 300s); the loop bails early if the client leaves. `0` disables the limit. |
| `PROXY_UPSTREAM_RCVBUF` | `4194304` (4 MiB) | `SO_RCVBUF` on upstream sockets — absorbs Ollama's post-thinking burst without backpressure stalling reads. |
| `PROXY_RECOVERY_FAILS` | `3` | Consecutive failed upstream attempts before the proxy aborts all in-flight upstreams to free a wedged Ollama slot (auto-recovery). |
| `PROXY_KEEPALIVE_INTERVAL` | `15` | Seconds of upstream silence before emitting an SSE keep-alive ping (`: keep-alive`) to reset the client's idle timer during deep thinking. Must be well under the client's idle timeout (~150s for VS Code). `0` disables pings. |
| `PROXY_BUSY_RETRY_BUDGET` | `120` | Total seconds (header phase) the proxy keeps re-issuing a request after fast retries are exhausted, waiting for a busy single slot to free up. Keep under the client's idle timeout (~150s). `0` disables patient retry (surface the 429/5xx immediately). |
| `PROXY_BUSY_RETRY_INTERVAL` | `30` | Seconds between patient-retry attempts while waiting for the slot to free. |

## Suggested Ollama environment

```bash
export OLLAMA_NUM_PARALLEL=1        # single generation slot (this proxy assumes it)
export OLLAMA_CONTEXT_LENGTH=82000
export OLLAMA_KEEP_ALIVE=24h
export OLLAMA_MAX_QUEUE=512
```

## Notes & limitations

- **Upstream encoding is forced to identity.** The proxy reads and reassembles the
  upstream body as text (SSE reassembly), so a compressed response would arrive as
  opaque bytes and silently break parsing. It therefore drops any client
  `Accept-Encoding` header and sends `Accept-Encoding: identity` toward Ollama,
  guaranteeing an uncompressed body regardless of what the client asked for.
- **Chunked request bodies are not supported** — the proxy returns `411` rather
  than forward an empty body. VS Code / Node always send `Content-Length` for
  JSON POSTs, so this is defensive only.
- Retries only happen **before any response byte is sent** to the client; once
  streaming starts a re-issue isn't possible (kill-and-respawn covers the
  non-streaming case where nothing was sent yet).
- The proxy binds `0.0.0.0` — fine for a local dev container, but don't expose
  it unauthenticated on an open network.
