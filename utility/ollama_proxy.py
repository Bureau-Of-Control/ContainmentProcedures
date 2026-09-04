#!/usr/bin/env python3
"""Ollama VS Code BYOM payload proxy.

Rewrites every incoming request so that it always targets a single fixed
model, regardless of the model ID the client (VS Code BYOM) sends. This lets
you register multiple BYOM entries pointing at this proxy with different
model IDs while all hit the same local Ollama model — only the per-entry
parameters differ.

Thinking depth: VS Code BYOM sends the entry "id" as the request "model", so
the ID itself can carry a directive that the proxy decodes into
reasoning_effort (the parameter Ollama's OpenAI-compat endpoint actually
honours — top-level "think" is silently ignored there):
    qwen3-coder-nothink      -> reasoning_effort "none"
    qwen3-coder-think-low    -> "low"   (also medium / high / max)

Optional: filters Windows-native tools out of POST /v1/chat/completions
payloads so they never enter the model context (Linux-only environments).

Usage:
    python3 ollama_proxy.py --model qwen3-coder \
        [--ollama-url http://127.0.0.1:11434] \
        [--port 8085] \
        [--default-effort none|low|medium|high|max] \
        [--filter-windows-tools] \
        [--log-file ollama_proxy.log]

All log output (per-request lines, config banner, crash tracebacks) is
duplicated to --log-file in addition to the console.
"""

import argparse
import json
import os
import queue
import re
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error


class _PrependReader:
    """Read wrapper that re-delivers a previously-read byte before the rest.

    Used by handle_one_request: we read 1 byte to detect TLS, and if it's
    not TLS we need to hand it back to BaseHTTPRequestHandler which reads
    the request line from self.rfile. This class prepends that byte.
    """
    def __init__(self, prepend: bytes, underlying):
        self._buf = prepend
        self._underlying = underlying

    def read(self, size=-1):
        if self._buf:
            data = self._buf
            self._buf = b""
            return data[:size] if size >= 0 else data
        return self._underlying.read(size)

    def readline(self, length=None):
        if self._buf:
            first_byte = self._buf
            self._buf = b""
            rest = self._underlying.readline(length)
            return first_byte + rest
        return self._underlying.readline(length)

    def __getattr__(self, name):
        return getattr(self._underlying, name)


class _Tee:
    """Write to several streams (console + log file), flushing each write so
    crashes are never lost in a buffer."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
        # Flush immediately so log lines (and crash tracebacks) are on disk
        # even if the process dies before a buffer would drain naturally.
        self.flush()
        return len(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass

# Hop-by-hop headers that must not be forwarded between client and backend.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# Windows-native tool patterns (case-insensitive, word-bounded so that e.g.
# "command" or "cmdline" are NOT matched).
WINDOWS_TOOL_PATTERNS = re.compile(
    r"\b(powershell|pwsh|cmd(?:\.exe)?|win32|windows)\b", re.IGNORECASE
)

# Thinking-depth directives encoded into the BYOM model ID (VS Code sends the
# entry "id" as the request "model"). Examples:
#   qwen3-coder-nothink      -> reasoning_effort "none"
#   qwen3-coder-think-low    -> "low"  (also medium / high / max)
THINK_DIRECTIVE = re.compile(
    r"[-_](nothink|think[-_](none|low|medium|high|max))$", re.IGNORECASE
)

VALID_EFFORTS = ("none", "low", "medium", "high", "max")

# Socket timeout (seconds) for the upstream Ollama connection during CONNECT
# and HEADER phases. Once headers arrive, _iter_upstream_lines switches the
# socket to blocking mode (no timeout), so long thinking silences in the body
# phase are simply "no data yet" — client liveness is polled instead. This
# matters when the CLIENT has gone away (aborted request): without it the
# handler thread would sit blocked in urlopen() forever, holding its place in
# Ollama's single-slot queue and starving every new request behind it.
UPSTREAM_READ_TIMEOUT = float(os.environ.get("PROXY_UPSTREAM_TIMEOUT", "30"))

# How many times to retry a failed upstream connection (Ollama down, socket
# reset while queued) before giving up. Retries only happen BEFORE any response
# byte has been sent to the client — once streaming starts we cannot re-issue
# the request. Also bounds kill-and-respawn attempts for non-streaming requests
# whose reader worker dies before any data arrived (see do_POST).
UPSTREAM_RETRY_ATTEMPTS = int(os.environ.get("PROXY_UPSTREAM_RETRIES", "3"))
UPSTREAM_RETRY_BACKOFF = float(os.environ.get("PROXY_UPSTREAM_BACKOFF", "1.0"))

# Patient retry for "slot busy" responses (429 / 5xx) in the HEADER phase.
# Ollama runs single-slot here; when a previous generation is still finishing
# and an overlapping request arrives, it rejects the new one with 503. Rather
# than surfacing that to VS Code immediately, keep the client waiting and
# re-issue until the slot frees up. Bounded by a total budget (kept under VS
# Code's ~150s idle timeout — beyond that it disconnects regardless) and an
# interval between attempts; aborted early if the client leaves. 0 disables
# patient retry (falls back to surfacing the error right away).
BUSY_RETRY_BUDGET = float(os.environ.get("PROXY_BUSY_RETRY_BUDGET", "120"))
BUSY_RETRY_INTERVAL = float(os.environ.get("PROXY_BUSY_RETRY_INTERVAL", "30"))

# Max time (seconds) to wait for Ollama's response HEADERS before giving up.
# This bounds the "queue wait" phase only — NOT generation duration (streaming
# is unbounded, as always). With a single slot, Ollama ACCEPTS an overlapping
# request and silently queues it behind the running generation (observed: a
# compaction request queued 120s+ behind a 4m28s generation), so this deadline
# must comfortably exceed typical long-generation durations or we fail the
# request with a bare 503 while the slot frees up moments later. The wait loop
# polls client liveness every 0.5s and bails early if the client leaves, so a
# generous value costs nothing when the client's own header timeout is shorter.
# Keep it at/under the client's headers timeout (undici default: 300s).
# 0 disables the limit.
MAX_QUEUE_WAIT = float(os.environ.get("PROXY_MAX_QUEUE_WAIT", "300"))

# Generous receive buffer for upstream sockets. After a long thinking phase
# Ollama flushes the entire answer in one burst; a larger SO_RCVBUF lets the
# kernel absorb it without backpressure stalling our read loop. 4 MB is well
# within "reasonable" for a local proxy and covers even very long responses.
UPSTREAM_RCVBUF = int(os.environ.get("PROXY_UPSTREAM_RCVBUF", str(4 * 1024 * 1024)))

# Keep-alive ping interval (seconds) for the CLIENT-facing SSE stream. During
# deep thinking / context compaction Ollama can emit no bytes for minutes; a
# client with an idle timeout (VS Code ~150s) will then close its connection,
# which makes us abort the in-flight generation and lose the answer. Emitting
# an SSE comment line (`: keep-alive`) every KEEPALIVE_INTERVAL seconds of
# upstream silence resets the client's idle timer so it stays connected while
# Ollama works. Comment lines are ignored by conforming SSE parsers, so they
# never corrupt the stream. 0 disables pings. Must be well under the client's
# idle timeout (15s is safely below typical 30-150s limits without spamming).
KEEPALIVE_INTERVAL = float(os.environ.get("PROXY_KEEPALIVE_INTERVAL", "15"))


class _UpstreamHTTPHandler(urllib.request.HTTPHandler):
    """urllib handler that sets a generous SO_RCVBUF on upstream sockets."""

    def http_open(self, req):
        resp = super().http_open(req)
        try:
            sock = None
            fp = getattr(resp, "fp", None)
            if fp is not None:
                raw = getattr(fp, "raw", None)
                if raw is not None:
                    sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UPSTREAM_RCVBUF)
        except (AttributeError, OSError):
            pass  # best-effort; never break the request over buffer tuning
        return resp


_UPSTREAM_OPENER = urllib.request.build_opener(_UpstreamHTTPHandler())

# Registry of in-flight upstream connections (for /_abort and graceful
# shutdown). Thread-safe: only mutated under _ACTIVE_LOCK.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_CONNECTIONS: dict[str, urllib.response.addinfourl] = {}

# --- Auto-recovery from a wedged upstream ---------------------------------
# With OLLAMA_NUM_PARALLEL=1 a single abandoned generation can hold the only
# slot, so every later request queues until MAX_QUEUE_WAIT and fails — even
# after Ollama itself recovers. We track consecutive failed requests; once the
# streak crosses a threshold we proactively abort ALL in-flight upstreams to
# free any wedged slot (the "re-establish connectivity with Ollama" path). A
# successful request resets the streak.
_UPSTREAM_FAIL_STREAK = 0
_UPSTREAM_FAIL_LOCK = threading.Lock()
RECOVERY_FAIL_THRESHOLD = int(os.environ.get("PROXY_RECOVERY_FAILS", "3"))


def _log(msg: str) -> None:
    """Emit one timestamped [proxy] log line.

    Every proxy log entry is prefixed with a local date+time so that entries
    from different requests (and restarts) can be correlated in the log file
    without guessing at ordering. stdout is tee'd to --log-file, so this lands
    on both the console and disk.
    """
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [proxy] {msg}")


def _note_upstream_failure():
    """Record a failed upstream attempt; returns True if recovery (abort-all)
    should be triggered this time."""
    global _UPSTREAM_FAIL_STREAK
    with _UPSTREAM_FAIL_LOCK:
        _UPSTREAM_FAIL_STREAK += 1
        streak = _UPSTREAM_FAIL_STREAK
    if streak >= RECOVERY_FAIL_THRESHOLD:
        _log(f"{streak} consecutive upstream failures — "
             f"aborting in-flight upstreams to free any wedged Ollama slot")
        abort_all_upstreams("auto-recovery after repeated failures")
        return True
    return False


def _note_upstream_success():
    global _UPSTREAM_FAIL_STREAK
    with _UPSTREAM_FAIL_LOCK:
        if _UPSTREAM_FAIL_STREAK:
            _log(f"upstream recovered (was {_UPSTREAM_FAIL_STREAK} failed in a row)")
        _UPSTREAM_FAIL_STREAK = 0


def abort_all_upstreams(reason):
    """Close every in-flight upstream connection so Ollama aborts its
    generation(s) and frees the slot immediately. Returns count closed."""
    with _ACTIVE_LOCK:
        conns = list(_ACTIVE_CONNECTIONS.items())
        _ACTIVE_CONNECTIONS.clear()
    for rid, resp in conns:
        try:
            resp.close()
        except Exception:
            pass
    if conns:
        _log(f"aborted {len(conns)} upstream connection(s): {reason}")
    return len(conns)


def think_directive(model_id):
    """Decode a thinking-depth directive from a BYOM model ID, or None."""
    m = THINK_DIRECTIVE.search(str(model_id))
    if not m:
        return None
    if m.group(1).lower() == "nothink":
        return "none"
    return (m.group(2) or "none").lower()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Proxy that forces all requests to a single Ollama model."
    )
    parser.add_argument(
        "--ollama-url",
        default="http://127.0.0.1:11434",
        help="Base URL of the backend Ollama service "
             "(default: http://127.0.0.1:11434)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8085,
        help="Port this proxy listens on (default: 8085)",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Target model ID to force into every request (mandatory)",
    )
    parser.add_argument(
        "--filter-windows-tools",
        action="store_true",
        default=False,
        help="Strip Windows-native tools (*powershell*, *cmd*, *win32*) from "
             "POST /v1/chat/completions payloads; keep Linux/POSIX tools only",
    )
    parser.add_argument(
        "--default-effort",
        choices=VALID_EFFORTS,
        default=None,
        help="reasoning_effort to inject when the model ID carries no "
             "-nothink / -think-<level> directive (default: none injected)",
    )
    parser.add_argument(
        "--log-file",
        default="ollama_proxy.log",
        help="Duplicate all log output (stdout+stderr) to this file, "
             "including crash tracebacks (default: ollama_proxy.log; "
             "pass /dev/null to disable)",
    )
    return parser.parse_args()


def tool_is_windows(tool):
    """Return True if a tool definition matches Windows-native patterns."""
    if not isinstance(tool, dict):
        return False
    # OpenAI/Ollama schema nests the function under "function"; also accept
    # top-level name/description for robustness.
    fn = tool.get("function")
    if not isinstance(fn, dict):
        fn = {}
    name = str(tool.get("name", "") or fn.get("name", ""))
    desc = str(tool.get("description", "") or fn.get("description", ""))
    return bool(WINDOWS_TOOL_PATTERNS.search(name)
                or WINDOWS_TOOL_PATTERNS.search(desc))


def filter_windows_tools(payload):
    """Remove Windows-native tools from a chat-completions payload in place."""
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return 0

    kept = [t for t in tools if not tool_is_windows(t)]
    removed = len(tools) - len(kept)
    if removed == 0:
        return 0

    payload["tools"] = kept
    # If nothing is left, drop the field entirely and any tool_choice that
    # references a now-missing Windows tool.
    if not kept:
        payload.pop("tools", None)
        val = payload.get("tool_choice")
        if isinstance(val, dict):
            fn = val.get("function")
            name = str(val.get("name", "") or (fn.get("name", "") if isinstance(fn, dict) else ""))
            if WINDOWS_TOOL_PATTERNS.search(name):
                payload.pop("tool_choice", None)
    return removed


class RewriteProxyHandler(BaseHTTPRequestHandler):
    # Config injected from the CLI (set on the class before serving).
    target_model = "qwen3-coder"
    ollama_url = "http://127.0.0.1:11434"
    filter_windows_tools_enabled = False
    default_effort = None

    # Generous I/O buffers for the client-facing socket (default wbufsize is
    # 0 = unbuffered, one syscall per line). A larger write buffer batches
    # bursty token output into fewer syscalls; we still flush after every SSE
    # line in the streaming loop so tokens reach VS Code immediately.
    rbufsize = 1024 * 1024   # 1 MiB read buffer (request bodies)
    wbufsize = 256 * 1024   # 256 KiB write buffer (response streaming)

    def _forward_headers(self):
        # Drop hop-by-hop headers, Host, and Content-Length (urllib recomputes
        # it from the actual body — forwarding the client's value would be
        # wrong whenever we rewrite the payload).
        skip = HOP_BY_HOP | {"host", "content-length"}
        return {
            k: v for k, v in self.headers.items() if k.lower() not in skip
        }

    def _send_error_body(self, code, message):
        body = json.dumps({"error": {"message": message}}).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client already gone — nothing to deliver.
            pass

    def _client_alive(self):
        """Best-effort check that the client socket still has a connection.

        Returns True when we cannot tell (treat as alive — never drop a live
        request on a false negative). A non-blocking peek of 1 byte is safe:
        it does not consume data, and EOF/error means the peer closed or
        reset the connection.

        CRITICAL: this probe must NEVER raise. It runs inside the request
        path (queue-wait join loop, streaming liveness poll); an uncaught
        exception here used to escape as AttributeError ('NoneType' ... 'peek')
        and kill the handler thread mid-flight, wedging the proxy. So we guard
        a None/closed socket and swallow every other error, defaulting to a
        safe answer (dead -> abort upstream; unknown -> alive).
        """
        sock = self.connection
        if sock is None:
            return False  # handler already torn down — treat as gone
        try:
            old_flags = sock.getblocking() if hasattr(sock, "getblocking") else None
            try:
                sock.setblocking(False)
                data = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
            finally:
                if old_flags is not None:
                    sock.setblocking(old_flags)
            return data != b""  # b"" == clean EOF — client closed the connection
        except (BlockingIOError, InterruptedError):
            return True       # no pending data — normal for an idle keep-alive
        except Exception:     # noqa: BLE001 — a probe must never raise into the request path
            return False      # RST / dead / closed socket — treat as gone

    def _iter_upstream_lines(self, response, poll_interval=5.0):
        """Yield upstream body lines while the client stays alive.

        Reads happen in a background worker thread with NO socket read
        timeout: a long thinking silence is simply "no data yet", and —
        critically — a buffered reader that times out mid-read gets poisoned
        (the next read raises OSError('cannot read from timed out object'),
        which crashed handler threads in the old design). The main loop polls
        client liveness every poll_interval seconds; if the client is gone it
        stops consuming, and the caller's finally-block closes the upstream
        response — aborting Ollama's generation and freeing its slot.

        While the client stays alive but Ollama is silent (deep thinking /
        context compaction), a 'ping' sentinel is yielded every
        KEEPALIVE_INTERVAL seconds so the caller can emit an SSE keep-alive to
        reset the client's idle timer and prevent it from dropping us mid-
        generation.

        Yields (kind, value) where kind is 'line' | 'error' | 'done' | 'ping'.
        """
        def _clear_read_timeout(resp):
            # Best-effort: drop the upstream socket read timeout now that
            # headers have arrived, so thinking silence blocks forever
            # instead of raising. (addinfourl.fp -> HTTPResponse.fp ->
            # BufferedReader.raw._sock for plain-HTTP Ollama.)
            target = resp.fp if hasattr(resp, "fp") else resp
            try:
                sock = target.fp.raw._sock  # noqa: SLF001
                if sock is not None:
                    sock.settimeout(None)
            except (AttributeError, OSError):
                pass  # keep existing timeout; the error path still recovers

        _clear_read_timeout(response)

        q: "queue.Queue" = queue.Queue()

        def _reader():
            try:
                for raw in response:  # HTTPResponse iterates by lines
                    q.put(("line", raw))
                q.put(("done", None))
            except BaseException as e:  # noqa: BLE001 — report to main thread
                q.put(("error", e))

        threading.Thread(target=_reader, daemon=True).start()
        last_activity = time.monotonic()
        while True:
            try:
                kind, value = q.get(timeout=poll_interval)
            except queue.Empty:
                if not self._client_alive():
                    _log(f"no upstream data for {poll_interval:.0f}s "
                         f"and client gone; aborting to free the Ollama slot")
                    return
                # Still thinking / compacting — keep waiting, socket stays healthy.
                # But if we've been silent long enough that a client with an idle
                # timeout (VS Code ~150s) would drop us, emit a keep-alive ping so
                # the caller can reset the client's timer and keep the connection
                # alive while Ollama works. Without this, VS Code closes its side
                # mid-generation, we see EOF, abort upstream, and lose the answer.
                if (KEEPALIVE_INTERVAL > 0
                        and (time.monotonic() - last_activity) >= KEEPALIVE_INTERVAL):
                    yield ("ping", None)
                    last_activity = time.monotonic()
                continue
            last_activity = time.monotonic()
            yield kind, value
            if kind in ("done", "error"):
                return

    def _consume_sse_as_nonstream(self, response):
        """Drain a forced-streaming upstream SSE body and reassemble it into
        the single JSON body a non-streaming client expects.

        Used when we rewrote stream=false -> stream=true (see do_POST) so that
        long thinking phases happen in the streaming phase instead of blocking
        the header phase. Returns (status, body_bytes, interrupted). On any
        read error or client disconnect the upstream is aborted (caller's
        finally closes it); `interrupted` tells the caller whether a fresh
        re-issue is worth attempting (nothing had been accumulated yet).
        """
        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason = None
        usage = None
        model_name = self.target_model
        stream_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        saw_sse_data = False

        def _accumulate(chunk):
            nonlocal finish_reason, usage, model_name
            if not isinstance(chunk, dict):
                return
            if chunk.get("model"):
                model_name = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    content_parts.append(piece)
                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    idx = int(tc.get("index", 0))
                    slot = tool_calls.setdefault(idx, {"id": "", "type": "function",
                                                      "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]

        raw_line = b""
        interrupted = False
        for kind, value in self._iter_upstream_lines(response):
            if kind == "done":
                break  # sentinel — upstream stream finished cleanly
            if kind == "error":
                _log(f"upstream read error: {type(value).__name__}: {value}")
                interrupted = True
                break
            if kind == "ping":
                continue  # keep-alive sentinel — nothing to accumulate
            line = value.decode("utf-8", "replace").strip()
            raw_line = value
            if not line.startswith("data:"):
                continue  # skip event:/id:/comment/blank lines
            data = line[5:].strip()
            if data == "[DONE]":
                break
            saw_sse_data = True
            try:
                _accumulate(json.loads(data))
            except ValueError:
                pass  # tolerate keep-alive or malformed chunks

        aborted = (finish_reason is None and not content_parts
                   and not tool_calls)
        if aborted and not saw_sse_data and response.status >= 400:
            # Ollama answered with a plain error body, not an SSE stream —
            # surface its real status code instead of masking it as 502.
            return (response.status,
                    raw_line or b'{"error": {"message": "upstream error"}}',
                    False)
        if aborted:
            return (502, json.dumps(
                {"error": {"message": "upstream stream interrupted during "
                                      "non-streaming reassembly"}},
                ensure_ascii=False).encode("utf-8"),
                interrupted)

        message: dict = {"role": "assistant", "content": "".join(content_parts)}
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        body_obj = {
            "id": stream_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish_reason or "stop"}],
        }
        if usage:
            body_obj["usage"] = usage
        return (response.status,
                json.dumps(body_obj, ensure_ascii=False).encode("utf-8"),
                False)  # completed cleanly — no re-issue needed

    def _forward_with_retry(self, req_factory):
        """Open the upstream request (see _forward_with_retry_inner), and feed
        the outcome into the auto-recovery tracker: a failure increments the
        consecutive-failure streak (triggering an abort-all once it crosses
        RECOVERY_FAIL_THRESHOLD to free any wedged Ollama slot); a success
        resets it."""
        resp = self._forward_with_retry_inner(req_factory)
        if resp is None:
            _note_upstream_failure()
        else:
            _note_upstream_success()
        return resp

    def _forward_with_retry_inner(self, req_factory):
        """Open the upstream request, retrying connection-level failures.

        Only called BEFORE any response byte is sent to the client, so a
        re-issue is safe. Retries cover: Ollama briefly unreachable (restart)
        and connection reset while queued. Body reads DURING streaming are
        handled separately by _iter_upstream_lines (background worker with no
        socket timeout + client-liveness polling), so they never surface here.

        Returns an open HTTPResponse on success, or None if all attempts
        failed (an error response has already been sent to the client).
        """
        last_exc = None
        for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
            try:
                if MAX_QUEUE_WAIT > 0:
                    # Overall deadline for getting response HEADERS (the
                    # "queue wait" phase). urlopen blocks until headers arrive;
                    # a per-read socket timeout alone would let it stall
                    # indefinitely across many slow reads. Run in a worker
                    # thread and join with the remaining budget. If we time
                    # out, the worker is abandoned (daemon) — Ollama will
                    # either serve it (harmless; steering preempts it) or drop
                    # it when its client connection dies.
                    box: dict = {}

                    def _open():
                        try:
                            # Socket timeout must cover the full queue wait +
                            # header read so the join budget below governs,
                            # not a premature socket timeout.
                            box["resp"] = _UPSTREAM_OPENER.open(
                                req_factory(),
                                timeout=MAX_QUEUE_WAIT + UPSTREAM_READ_TIMEOUT)
                        except BaseException as e:  # noqa: BLE001 — report to main thread
                            box["exc"] = e

                    worker = threading.Thread(target=_open, daemon=True)
                    worker.start()
                    deadline = time.monotonic() + MAX_QUEUE_WAIT
                    while worker.is_alive():
                        if not self._client_alive():
                            # Client gone — abandon our place in the queue and
                            # free the slot. No error body to send (no one's
                            # listening).
                            _log("client disconnected during queue "
                                 "wait; abandoning upstream slot")
                            abort_all_upstreams("client disconnected (queue wait)")
                            worker.join(timeout=2)
                            return None
                        if time.monotonic() > deadline:
                            break
                        worker.join(timeout=0.5)
                    if "resp" in box:
                        return box["resp"]
                    if "exc" in box:
                        raise box["exc"]
                    # Still no headers after MAX_QUEUE_WAIT — give up. Log it:
                    # this path previously emitted a bare 503 with zero markers,
                    # which made "queued behind a long generation" indistinguishable
                    # from other failures in the log.
                    _log(f"upstream did not respond within "
                         f"{MAX_QUEUE_WAIT:.0f}s — request was likely queued "
                         f"behind an in-flight generation (single slot); "
                         f"surfacing 503 to client")
                    self._send_error_body(
                        503, f"upstream did not respond within {MAX_QUEUE_WAIT:.0f}s; "
                             f"a generation may be holding the slot — steer or /_abort")
                    return None
                return _UPSTREAM_OPENER.open(req_factory(), timeout=UPSTREAM_READ_TIMEOUT)
            except urllib.error.HTTPError as e:
                # Transient server errors (429 / 5xx) — Ollama returns these
                # when the slot is busy or it aborts a queued request. Retry
                # with backoff; the long generation will finish and free the
                # slot. Client errors (4xx other than 429) are permanent —
                # surface immediately, retrying just wastes time.
                if e.code == 429 or e.code >= 500:
                    last_exc = e
                    # Single-slot "busy" case: Ollama rejects an overlapping
                    # request with a hard 5xx while a previous generation is
                    # still finishing. This is the common failure, so it gets
                    # its own handler (independent of the fast-retry attempt
                    # count). Quick backoff first; if that doesn't clear it and
                    # the client is still waiting, fall to a slow patient retry
                    # until the slot frees — bounded by a total budget (kept
                    # under the client's ~150s idle timeout) and aborted early
                    # if the client leaves.
                    if attempt < UPSTREAM_RETRY_ATTEMPTS and self._client_alive():
                        delay = UPSTREAM_RETRY_BACKOFF * attempt
                        _log(f"upstream {e.code} (attempt "
                             f"{attempt}/{UPSTREAM_RETRY_ATTEMPTS}) — retrying in {delay:.1f}s")
                        time.sleep(delay)
                        continue
                    if BUSY_RETRY_BUDGET > 0 and self._client_alive():
                        deadline = time.monotonic() + BUSY_RETRY_BUDGET
                        _log(f"upstream {e.code} — slot busy; patient "
                             f"retry up to {BUSY_RETRY_BUDGET:.0f}s (every "
                             f"{BUSY_RETRY_INTERVAL:.0f}s) while client waits")
                        while time.monotonic() < deadline:
                            if not self._client_alive():
                                # Our client left. The busy slot belongs to
                                # another connection's generation — leave it
                                # alone (it may be a live one) and just stop
                                # waiting; it will free up on its own.
                                _log("client left during patient retry; stopping wait")
                                return None
                            time.sleep(BUSY_RETRY_INTERVAL)
                            try:
                                resp = _UPSTREAM_OPENER.open(
                                    req_factory(), timeout=MAX_QUEUE_WAIT + UPSTREAM_READ_TIMEOUT)
                                _log(f"patient retry succeeded (status={resp.status})")
                                return resp
                            except urllib.error.HTTPError as e2:
                                last_exc = e2  # still busy — keep waiting
                            except (urllib.error.URLError, socket.timeout,
                                    ConnectionError, TimeoutError, OSError) as e2:
                                last_exc = e2  # transient — keep waiting
                        _log(f"patient retry budget ({BUSY_RETRY_BUDGET:.0f}s) "
                             f"exhausted on {last_exc}")
                    break  # permanent error, or retries+budget exhausted
                # Permanent error (4xx other than 429) — surface it.
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return None
            except (urllib.error.URLError, socket.timeout,
                    ConnectionError, TimeoutError, OSError) as e:
                last_exc = e
                if attempt < UPSTREAM_RETRY_ATTEMPTS and self._client_alive():
                    delay = UPSTREAM_RETRY_BACKOFF * attempt
                    _log(f"upstream error (attempt {attempt}/{UPSTREAM_RETRY_ATTEMPTS}): "
                         f"{type(e).__name__}: {e} — retrying in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    reason = ("client gone" if not self._client_alive()
                              else f"retries exhausted ({UPSTREAM_RETRY_ATTEMPTS})")
                    _log(f"giving up on upstream request: "
                         f"{type(e).__name__}: {e} — {reason}")
                    break
        self._send_error_body(502, f"upstream Ollama unreachable after retries: {last_exc}")
        return None

    def do_POST(self):
        # --- Control endpoint: abort all in-flight upstream requests. ---
        if self.path.rstrip("/") == "/_abort":
            n = abort_all_upstreams("manual /_abort request")
            body = json.dumps({"aborted": n}).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        # --- Request identity + timing (for log correlation with ollama.log). ---
        rid = uuid.uuid4().hex[:8]
        t_received = time.monotonic()

        # Chunked request bodies are not supported: without a Content-Length
        # we would read 0 bytes and forward an empty body (confusing Ollama
        # 400). Fail loudly instead. VS Code / Node always send Content-Length
        # for JSON POSTs, so this is defensive only.
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._send_error_body(411, "chunked transfer-encoding not supported")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_error_body(400, "missing or invalid Content-Length header")
            return
        post_data = self.rfile.read(content_length)

        # True when we forced stream=true upstream for a client that asked for
        # a single (non-streaming) JSON body — the response must then be
        # reassembled from SSE chunks before being sent back.
        forced_stream = False

        modified_data = post_data
        try:
            payload = json.loads(post_data.decode("utf-8"))
            if isinstance(payload, dict):
                # Decode a thinking-depth directive from the BYOM model ID
                # (e.g. qwen3-coder-nothink, qwen3-coder-think-low), then force
                # the real model name Ollama expects.
                requested = payload.get("model")
                if isinstance(requested, str):
                    effort = think_directive(requested) or self.default_effort
                    if effort:
                        payload["reasoning_effort"] = effort
                        _log(f"thinking directive from {requested!r} "
                             f"-> reasoning_effort={effort}")

                # Force the model to be exactly what Ollama expects.
                if "model" in payload:
                    payload["model"] = self.target_model

                # KEY FIX (long-thinking stall): when the client asks for a
                # NON-streaming chat completion, Ollama generates the ENTIRE
                # reply — including all internal thinking tokens — before
                # sending a single byte back. That silence in the HEADER phase
                # is what killed the old proxy: three "TimeoutError" aborts at
                # 30.029s while Ollama was happily thinking for ~1m44s, and it
                # would also trip MAX_QUEUE_WAIT on longer thoughts.
                #
                # Forcing stream=true makes Ollama send HTTP headers IMMEDIATELY
                # on request acceptance, so the header phase is always fast and
                # the long thinking silence then happens in the streaming body
                # phase — where _iter_upstream_lines reads with no socket
                # timeout (background worker) and only polls client liveness.
                # We reassemble the SSE chunks into the single JSON body the
                # client expects.
                if (self.path.rstrip("/").endswith("/v1/chat/completions")
                        and not payload.get("stream")):
                    payload["stream"] = True
                    forced_stream = True

                # Strip Windows-native tools for chat completions only.
                if (self.filter_windows_tools_enabled
                        and self.path.rstrip("/").endswith("/v1/chat/completions")):
                    removed = filter_windows_tools(payload)
                    if removed:
                        _log(f"filtered {removed} Windows tool(s)")

            # ensure_ascii=False keeps non-ASCII log content byte-for-byte
            # instead of inflating it into \uXXXX escapes; output is UTF-8.
            modified_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            # Not JSON — forward untouched.
            pass

        # Rebuild the Request on every attempt: urllib consumes the data
        # buffer, so a retry needs a fresh object with the same body.
        def _make_request():
            return urllib.request.Request(
                f"{self.ollama_url}{self.path}",
                data=modified_data,
                headers=self._forward_headers(),
                method="POST",
            )

        response = self._forward_with_retry(_make_request)
        if response is None:
            return  # error already delivered to the client

        t_headers = time.monotonic()
        queue_wait = t_headers - t_received
        _log(f"{rid} headers in {queue_wait:.1f}s (status={response.status})")

        # Register for /_abort and graceful shutdown.
        with _ACTIVE_LOCK:
            _ACTIVE_CONNECTIONS[rid] = response

        try:
            if forced_stream:
                # We rewrote stream=false -> stream=true upstream (see above).
                # Drain the whole SSE body — tolerating long thinking silences
                # via _client_alive() — then deliver ONE JSON body, exactly
                # what a non-streaming client asked for.
                #
                # Kill-and-respawn: if the reader worker dies before ANY data
                # arrived (poisoned socket, Ollama dropped us mid-queue), we
                # have sent nothing to the client yet — so re-issue the request
                # fresh instead of failing it. Bounded by UPSTREAM_RETRY_ATTEMPTS.
                status = body = None
                for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
                    status, body, interrupted = self._consume_sse_as_nonstream(response)
                    if not interrupted:
                        # Clean completion, client gone, or a real HTTP error from
                        # upstream (all of which return interrupted=False). Only an
                        # interrupted reassembly — reader worker died before anything
                        # usable arrived — is safe to re-issue. (Note: the interrupted
                        # path returns status 502, so a `status >= 400` guard here
                        # would make the respawn below unreachable.)
                        break
                    _log(f"{rid} worker died before any data "
                         f"(attempt {attempt}/{UPSTREAM_RETRY_ATTEMPTS}) — respawning")
                    with _ACTIVE_LOCK:
                        _ACTIVE_CONNECTIONS.pop(rid, None)
                    response.close()  # aborts the dead upstream, frees its slot
                    if not self._client_alive():
                        break
                    time.sleep(UPSTREAM_RETRY_BACKOFF * attempt)
                    response = self._forward_with_retry(_make_request)
                    if response is None:
                        return  # error already delivered to the client
                    with _ACTIVE_LOCK:
                        _ACTIVE_CONNECTIONS[rid] = response
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    _log(f"{rid} client gone before final body; "
                         f"aborting upstream to free the Ollama slot")
            else:
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                        self.send_header(k, v)
                self.end_headers()

            if forced_stream:
                pass  # body already delivered above as a single JSON response
            else:
                # Stream line-by-line so SSE events are never split mid-chunk.
                # Reads happen in the background worker (no socket timeout),
                # so long thinking silences can't poison the reader; we only
                # poll client liveness between chunks.
                for kind, value in self._iter_upstream_lines(response):
                    if kind == "done":
                        break  # sentinel — upstream stream finished cleanly
                    if kind == "error":
                        _log(f"{rid} upstream read error: "
                             f"{type(value).__name__}: {value}")
                        break
                    if kind == "ping":
                        # Upstream is silent (thinking/compacting) but the client
                        # is still connected. Emit an SSE comment line — ignored by
                        # conforming parsers, but it resets the client's idle timer
                        # so VS Code doesn't drop us mid-generation and force Ollama
                        # to abort a perfectly good in-flight answer.
                        try:
                            self.wfile.write(b": keep-alive\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            _log(f"{rid} client gone during keep-alive; "
                                 f"aborting upstream to free the Ollama slot")
                            break
                        continue
                    line = value
                    if not line.endswith(b"\n"):
                        line += b"\n"
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()  # deliver each SSE chunk immediately
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        # Client disconnected mid-stream. Break out immediately so
                        # the finally closes the upstream response — that aborts
                        # Ollama's generation and frees its slot for the next
                        # request instead of holding it until the model finishes
                        # a response nobody is reading.
                        _log(f"{rid} client disconnected mid-stream; "
                             f"aborting upstream to free the Ollama slot")
                        break
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_CONNECTIONS.pop(rid, None)
            if response is not None:
                response.close()
            total = time.monotonic() - t_received
            _log(f"{rid} done in {total:.1f}s "
                 f"(queue_wait={t_headers - t_received:.1f}s)")

    def do_GET(self):
        # Proxy liveness endpoint — does NOT touch Ollama, so it works even
        # when the upstream is wedged. Handy for monitoring / auto-restart:
        #   curl http://localhost:8050/_health
        if self.path == "/_health":
            with _ACTIVE_LOCK:
                active = len(_ACTIVE_CONNECTIONS)
            with _UPSTREAM_FAIL_LOCK:
                streak = _UPSTREAM_FAIL_STREAK
            payload = json.dumps({
                "status": "ok",
                "active_upstreams": active,
                "fail_streak": streak,
                "recovery_threshold": RECOVERY_FAIL_THRESHOLD,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # Pass through GET requests (e.g. /api/tags, /v1/models).
        req = urllib.request.Request(
            f"{self.ollama_url}{self.path}", method="GET"
        )
        try:
            with _UPSTREAM_OPENER.open(req, timeout=UPSTREAM_READ_TIMEOUT) as response:
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(response.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    def finish(self):
        """Override socketserver's finish() to suppress BrokenPipeError.

        When the client disconnects mid-stream (aborted request, VS Code
        timeout, etc.), we break out of the streaming loop and log a clean
        message. But then socketserver calls wfile.close(), which tries to
        flush remaining buffered bytes into a dead socket — raising
        BrokenPipeError that propagates through process_request_thread and
        dumps a full traceback via sys.excepthook. The disconnect is already
        logged by our streaming loop; the traceback adds nothing but noise.
        """
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client already gone — expected, already logged

    def handle_one_request(self):
        """Override to detect TLS bytes sent to this plain-HTTP proxy.

        VS Code (or a retry mechanism) occasionally sends an HTTPS/TLS
        ClientHello to our HTTP port. The first byte of a TLS record is
        0x16 (handshake) or 0x17 (application data). Without detection,
        BaseHTTPRequestHandler tries to parse it as an HTTP request line,
        fails, and logs the raw binary garbage as a 400 — polluting the log
        with unreadable bytes. We detect the TLS magic, send a clean 426
        (Upgrade Required) response, and close the connection quietly.

        Implementation: read the first byte; if it's not TLS, wrap rfile so
        that byte is re-delivered before the rest of the stream, then call
        super().handle_one_request() as normal.
        """
        try:
            first = self.rfile.read(1)
        except (ConnectionResetError, OSError):
            return  # client already gone
        if not first:
            return  # clean EOF — nothing to do
        if first[0] in (0x16, 0x17):
            # TLS record — this is an HTTPS client hitting our HTTP port.
            _log(f"{self.address_string()} - received TLS bytes on plain-HTTP "
                 f"port; sending 426 Upgrade Required")
            try:
                # send_response() needs requestline + request_version, which are
                # normally set by parsing the HTTP request line. Since we never
                # parsed a valid request, initialize them manually.
                self.requestline = b"<TLS>"
                self.request_version = "HTTP/1.1"
                self.send_response(426)
                self.send_header("Content-Type", "text/plain")
                body = b"This proxy speaks plain HTTP. Use http:// not https://"
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # client already gone
            # Prevent the parent handle() loop from trying to read another
            # request line on this connection.
            self.close_connection = True
            return
        # Not TLS — re-deliver the byte and proceed normally.
        self.rfile = _PrependReader(first, self.rfile)
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected while we were flushing the response. This is
            # expected on mid-stream aborts; the disconnect is already logged
            # by our streaming loop. Swallow it to avoid a traceback dump.
            pass

    def log_message(self, fmt, *args):
        _log(f"{self.address_string()} - {fmt % args}")


def main():
    args = parse_args()

    # Duplicate stdout/stderr to the log file so crashes and per-request logs
    # survive even when the console is gone. Every write flushes, so a crash
    # mid-line still lands in the file.
    try:
        log_file = open(args.log_file, "a", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, log_file)
        sys.stderr = _Tee(sys.__stderr__, log_file)
    except OSError as e:
        # Tee setup failed, so write straight to the real stderr with a manual
        # timestamp (same format _log uses) for consistency.
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [proxy] WARNING: "
              f"could not open log file {args.log_file!r}: {e}",
              file=sys.__stderr__)

    # Record uncaught exceptions (per-request handler crashes included) with
    # full tracebacks instead of just the one-line summary.
    def _excepthook(exc_type, exc_value, exc_tb):
        # Route through the (possibly tee'd) stderr so tracebacks also land
        # in the log file.
        sys.stderr.write("".join(traceback.format_exception(
            exc_type, exc_value, exc_tb)))
        sys.stderr.flush()

    sys.excepthook = _excepthook

    RewriteProxyHandler.target_model = args.model
    RewriteProxyHandler.ollama_url = args.ollama_url.rstrip("/")
    RewriteProxyHandler.filter_windows_tools_enabled = args.filter_windows_tools
    RewriteProxyHandler.default_effort = args.default_effort

    server = ThreadingHTTPServer(("0.0.0.0", args.port), RewriteProxyHandler)
    # Let handler threads die with the process instead of blocking shutdown
    # on a stuck client connection.
    server.daemon_threads = True

    # Graceful shutdown: on SIGTERM/SIGINT, actively close every in-flight
    # upstream connection so Ollama aborts its generation(s) immediately and
    # frees the slot — instead of letting daemon threads die while Ollama
    # keeps generating for a dead client until it notices the RST.
    _shutdown_started = {"flag": False}

    def _handle_shutdown(signum, frame):
        if _shutdown_started["flag"]:
            return  # second Ctrl-C — let the default handler kill us hard
        _shutdown_started["flag"] = True
        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] [proxy] signal "
              f"{signum} received — aborting upstreams and shutting down")
        abort_all_upstreams(f"signal {signum}")
        # shutdown() must be called from a different thread than serve_forever.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    # Timestamp the banner so restarts are easy to find in the log; the
    # indented sub-lines below are continuations of this line.
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Ollama VS Code BYOM proxy "
          f"running on 0.0.0.0:{args.port}")
    print(f"  -> backend:      {RewriteProxyHandler.ollama_url}")
    print(f"  -> target model: {args.model}")
    print(f"  -> default reasoning_effort: "
          f"{args.default_effort or '(none, only via -nothink/-think-<level> ID)'}")
    print(f"  -> filter Windows tools: {args.filter_windows_tools}")
    if args.log_file != "/dev/null":
        print(f"  -> log file:       {args.log_file} (stdout+stderr duplicated)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
