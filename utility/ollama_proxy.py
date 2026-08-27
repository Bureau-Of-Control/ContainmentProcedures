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
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error


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
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
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
                        print(f"[proxy] thinking directive from {requested!r} "
                              f"-> reasoning_effort={effort}")

                # Force the model to be exactly what Ollama expects.
                if "model" in payload:
                    payload["model"] = self.target_model

                # Strip Windows-native tools for chat completions only.
                if (self.filter_windows_tools_enabled
                        and self.path.rstrip("/").endswith("/v1/chat/completions")):
                    removed = filter_windows_tools(payload)
                    if removed:
                        print(f"[proxy] filtered {removed} Windows tool(s)")

            # ensure_ascii=False keeps non-ASCII log content byte-for-byte
            # instead of inflating it into \uXXXX escapes; output is UTF-8.
            modified_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            # Not JSON — forward untouched.
            pass

        req = urllib.request.Request(
            f"{self.ollama_url}{self.path}",
            data=modified_data,
            headers=self._forward_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as response:
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                        self.send_header(k, v)
                self.end_headers()

                # Stream line-by-line so SSE events are never split mid-chunk.
                for line in response:  # HTTPResponse iterates by lines
                    if not line.endswith(b"\n"):
                        line += b"\n"
                    self.wfile.write(line)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        # Pass through GET requests (e.g. /api/tags, /v1/models).
        req = urllib.request.Request(
            f"{self.ollama_url}{self.path}", method="GET"
        )
        try:
            with urllib.request.urlopen(req) as response:
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

    def log_message(self, fmt, *args):
        print(f"[proxy] {self.address_string()} - {fmt % args}")


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
        print(f"[proxy] WARNING: could not open log file {args.log_file!r}: {e}",
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
    print(f"Ollama VS Code BYOM proxy running on 0.0.0.0:{args.port}")
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
