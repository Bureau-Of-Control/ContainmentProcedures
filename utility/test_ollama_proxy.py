#!/usr/bin/env python3
"""Comprehensive unit + integration tests for ollama_proxy.py.

Covers every scenario that was previously debugged by hand:
  - Model rewrite & thinking directives (-nothink, -think-<level>, --default-effort)
  - Windows tool filtering (strip powershell/cmd/win32; drop empty tools + dangling tool_choice)
  - Forced-streaming reassembly (stream=false → true upstream; SSE → single chat.completion JSON)
  - Streaming passthrough (line-by-line flush)
  - Keep-alive pings during upstream silence
  - Client-disconnect mid-stream aborts upstream
  - Patient retry on header-phase 503/429 (flaky → forwarded after wait)
  - Client leaves during patient retry → clean stop, no abort of others
  - MAX_QUEUE_WAIT deadline (fake accepts but never sends headers in time → 503)
  - Client gone during queue wait → slot abandoned
  - Kill-and-respawn (worker dies before any data → re-issue succeeds)
  - Auto-recovery fail-streak + /_health shape
  - /_abort returns aborted count
  - Chunked TE → 411; invalid Content-Length → 400
  - GET passthrough (/api/tags); non-JSON body forwarded untouched
  - Permanent 4xx from upstream forwarded with real status (no retry)

Run:  python3 test_ollama_proxy.py [-v]
No third-party packages required — stdlib unittest only.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Import the proxy module directly (safe: main() is behind __main__ guard).
# ---------------------------------------------------------------------------
PROXY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ollama_proxy.py")
sys.path.insert(0, os.path.dirname(PROXY_PATH))
import ollama_proxy  # noqa: E402


# ===================================================================
# Helpers
# ===================================================================

def get_free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_post(url: str, body: bytes | None = None, headers: dict | None = None,
              timeout: float = 30) -> tuple[int, dict, bytes]:
    """POST and return (status, headers_dict, body_bytes)."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def http_get(url: str, timeout: float = 10) -> tuple[int, dict, bytes]:
    """GET and return (status, headers_dict, body_bytes)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def raw_http_request(host: str, port: int, request_bytes: bytes,
                     read_timeout: float = 10) -> tuple[int, bytes]:
    """Send raw HTTP bytes over a socket; return (status_code, response_body)."""
    sock = socket.create_connection((host, port), timeout=read_timeout)
    try:
        sock.sendall(request_bytes)
        # Read until we have headers + body (simple: read all available)
        chunks = []
        sock.settimeout(read_timeout)
        while True:
            try:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
            except socket.timeout:
                break
        raw = b"".join(chunks)
    finally:
        sock.close()
    # Parse status line
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        return 0, raw
    status_line = raw.split(b"\r\n", 1)[0].decode("utf-8", "replace")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 else 0
    body = raw[header_end + 4:]
    return status, body


def sse_chunks(*pieces: str) -> bytes:
    """Build an SSE byte stream from data pieces (each becomes a 'data:' line)."""
    out = b""
    for p in pieces:
        out += f"data: {p}\n\n".encode("utf-8")
    out += b"data: [DONE]\n\n"
    return out


def _wants_stream(body: bytes) -> bool:
    """True if the JSON request body asks for streaming (stream=true)."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("stream"))


# ===================================================================
# Fake Ollama server
# ===================================================================

class _FakeOllamaHandler(BaseHTTPRequestHandler):
    """Delegates request handling to the test's `on_request` callable."""

    def log_message(self, *args):  # silence default logging
        pass

    def _read_body(self) -> bytes:
        cl = self.headers.get("Content-Length")
        if cl is None:
            return b""
        try:
            n = int(cl)
        except ValueError:
            return b""
        return self.rfile.read(n) if n > 0 else b""

    def _dispatch(self, method: str):
        body = self._read_body()
        srv = self.server.fake  # FakeOllamaServer wrapper (see __init__)
        with srv.lock:
            srv.requests.append({
                "method": method,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            })
            srv.count += 1
            n = srv.count

        if srv.on_request is not None:
            srv.on_request(self, method, self.path, body, n)
        else:
            # Default behavior — mirror real Ollama. When the proxy forces
            # stream=true on /v1/chat/completions (which it always does for
            # non-streaming client requests), a real Ollama returns an SSE
            # stream, so we must too; otherwise _consume_sse_as_nonstream()
            # sees no SSE data and returns 502. Other paths get plain JSON.
            if self.path.rstrip("/").endswith("/v1/chat/completions"):
                chunk = json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
                resp = sse_chunks(chunk)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            else:
                resp = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

    def do_POST(self):
        self._dispatch("POST")

    def do_GET(self):
        self._dispatch("GET")


class FakeOllamaServer:
    """ThreadingHTTPServer wrapper with per-test behavior injection."""

    def __init__(self):
        self.port = get_free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), _FakeOllamaHandler)
        self.server.daemon_threads = True
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.count = 0
        self.on_request = None  # callable(handler, method, path, body, n)
        # Back-reference: inside the handler, `self.server` is this
        # ThreadingHTTPServer (NOT the FakeOllamaServer wrapper), so expose
        # the wrapper on it for _dispatch to reach.
        self.server.fake = self

    def start(self):
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def last_request(self) -> dict | None:
        with self.lock:
            return self.requests[-1] if self.requests else None

    def rst_close(self, handler):
        """Close the handler's connection with RST (not FIN)."""
        try:
            handler.wfile.flush()
        except Exception:
            pass
        try:
            sock = handler.connection
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                               struct.pack("ii", 1, 0))
                sock.close()
        except Exception:
            pass


# ===================================================================
# Proxy subprocess management
# ===================================================================

class ProxyProcess:
    """Manages a proxy subprocess with log capture."""

    def __init__(self, port: int, ollama_url: str, extra_env: dict | None = None,
                 extra_args: list[str] | None = None):
        self.port = port
        self.log_path = os.path.join(tempfile.mkdtemp(prefix="proxy_test_"), "proxy.log")

        env = os.environ.copy()
        # Sensible test defaults (fast retries, small budgets)
        env.setdefault("PROXY_UPSTREAM_RETRIES", "1")
        env.setdefault("PROXY_BUSY_RETRY_BUDGET", "0")
        env.setdefault("PROXY_BUSY_RETRY_INTERVAL", "1")
        env.setdefault("PROXY_MAX_QUEUE_WAIT", "300")
        env.setdefault("PROXY_KEEPALIVE_INTERVAL", "15")
        if extra_env:
            env.update(extra_env)

        cmd = [
            sys.executable, PROXY_PATH,
            "--model", "test-model",
            "--port", str(port),
            "--ollama-url", ollama_url,
            "--log-file", "/dev/null",
        ]
        if extra_args:
            cmd.extend(extra_args)

        with open(self.log_path, "w") as log_file:
            self.proc = subprocess.Popen(
                cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT,
            )

    def wait_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"proxy exited early (rc={self.proc.returncode}); "
                    f"log:\n{self.read_log()}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/_health", timeout=1
                ) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.1)
        raise TimeoutError(f"proxy did not become ready within {timeout}s")

    def read_log(self) -> str:
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)


# ===================================================================
# Base integration test class
# ===================================================================

class ProxyIntegrationBase(unittest.TestCase):
    """Starts a fake Ollama + proxy subprocess for each test."""

    def setUp(self):
        self.fake = FakeOllamaServer().start()
        self.proxy_port = get_free_port()
        # Subclasses set self.extra_env / self.extra_args before calling _start_proxy
        self.extra_env: dict | None = None
        self.extra_args: list[str] | None = None
        self._proxy: ProxyProcess | None = None

    def _start_proxy(self):
        self._proxy = ProxyProcess(
            port=self.proxy_port,
            ollama_url=self.fake.url,
            extra_env=self.extra_env,
            extra_args=self.extra_args,
        )
        self._proxy.wait_ready()
        return self._proxy

    def tearDown(self):
        if self._proxy is not None:
            self._proxy.stop()
        self.fake.stop()

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    def read_proxy_log(self) -> str:
        return self._proxy.read_log() if self._proxy else ""


# ===================================================================
# 1. UNIT TESTS — think_directive
# ===================================================================

class ThinkDirectiveTests(unittest.TestCase):
    """Direct tests of ollama_proxy.think_directive()."""

    def test_nothink(self):
        self.assertEqual(ollama_proxy.think_directive("qwen3-coder-nothink"), "none")

    def test_think_none(self):
        self.assertEqual(ollama_proxy.think_directive("model-think-none"), "none")

    def test_think_low(self):
        self.assertEqual(ollama_proxy.think_directive("model-think-low"), "low")

    def test_think_medium(self):
        self.assertEqual(ollama_proxy.think_directive("model-think-medium"), "medium")

    def test_think_high(self):
        self.assertEqual(ollama_proxy.think_directive("model-think-high"), "high")

    def test_think_max(self):
        self.assertEqual(ollama_proxy.think_directive("model-think-max"), "max")

    def test_underscore_variant(self):
        self.assertEqual(ollama_proxy.think_directive("model_think_low"), "low")

    def test_case_insensitive(self):
        self.assertEqual(ollama_proxy.think_directive("MODEL-THINK-HIGH"), "high")
        self.assertEqual(ollama_proxy.think_directive("Model-NoThink"), "none")

    def test_plain_model_no_directive(self):
        self.assertIsNone(ollama_proxy.think_directive("qwen3-coder"))

    def test_non_matching_suffix(self):
        # "thinker" does not match think-<level>
        self.assertIsNone(ollama_proxy.think_directive("model-thinker"))
        # Directive must be at the END of the string
        self.assertIsNone(ollama_proxy.think_directive("nothink-model"))

    def test_non_string_input(self):
        # Should not raise; str() conversion handles it
        self.assertIsNone(ollama_proxy.think_directive(12345))


# ===================================================================
# 2. UNIT TESTS — tool_is_windows / filter_windows_tools
# ===================================================================

class ToolIsWindowsTests(unittest.TestCase):
    """Direct tests of ollama_proxy.tool_is_windows()."""

    def test_powershell_name(self):
        self.assertTrue(ollama_proxy.tool_is_windows({"name": "powershell"}))

    def test_cmd_name(self):
        self.assertTrue(ollama_proxy.tool_is_windows({"name": "cmd"}))

    def test_win32_in_description(self):
        self.assertTrue(ollama_proxy.tool_is_windows(
            {"name": "run", "description": "Execute a win32 command"}))

    def test_windows_in_name(self):
        self.assertTrue(ollama_proxy.tool_is_windows({"name": "windows-helper"}))

    def test_nested_function(self):
        tool = {"type": "function", "function": {"name": "powershell"}}
        self.assertTrue(ollama_proxy.tool_is_windows(tool))

    def test_non_windows_bash(self):
        self.assertFalse(ollama_proxy.tool_is_windows({"name": "bash"}))

    def test_non_windows_python(self):
        self.assertFalse(ollama_proxy.tool_is_windows({"name": "python_exec"}))

    def test_word_boundary_command_not_matched(self):
        # "command" contains "cmd" but no word boundary → should NOT match
        self.assertFalse(ollama_proxy.tool_is_windows({"name": "command_runner"}))

    def test_word_boundary_cmdline_not_matched(self):
        self.assertFalse(ollama_proxy.tool_is_windows({"name": "cmdline_tool"}))

    def test_non_dict_input(self):
        self.assertFalse(ollama_proxy.tool_is_windows("powershell"))  # type: ignore
        self.assertFalse(ollama_proxy.tool_is_windows(None))          # type: ignore


class FilterWindowsToolsTests(unittest.TestCase):
    """Direct tests of ollama_proxy.filter_windows_tools()."""

    def test_mixed_tools_strips_windows(self):
        payload = {
            "tools": [
                {"type": "function", "function": {"name": "powershell"}},
                {"type": "function", "function": {"name": "bash"}},
            ]
        }
        removed = ollama_proxy.filter_windows_tools(payload)
        self.assertEqual(removed, 1)
        self.assertEqual(len(payload["tools"]), 1)
        self.assertEqual(payload["tools"][0]["function"]["name"], "bash")

    def test_all_windows_drops_tools_and_tool_choice(self):
        payload = {
            "tools": [
                {"type": "function", "function": {"name": "powershell"}},
                {"type": "function", "function": {"name": "cmd"}},
            ],
            "tool_choice": {"type": "function", "function": {"name": "powershell"}},
        }
        removed = ollama_proxy.filter_windows_tools(payload)
        self.assertEqual(removed, 2)
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_all_windows_keeps_non_matching_tool_choice(self):
        payload = {
            "tools": [{"type": "function", "function": {"name": "powershell"}}],
            "tool_choice": {"type": "function", "function": {"name": "bash"}},
        }
        removed = ollama_proxy.filter_windows_tools(payload)
        self.assertEqual(removed, 1)
        self.assertNotIn("tools", payload)
        # tool_choice references a non-Windows name → kept
        self.assertIn("tool_choice", payload)

    def test_list_tool_choice_all_windows_dropped(self):
        """List-form tool_choice whose entries are all Windows tools is dropped."""
        payload = {
            "tools": [
                {"type": "function", "function": {"name": "powershell"}},
                {"type": "function", "function": {"name": "cmd"}},
            ],
            "tool_choice": ["powershell", "cmd"],
        }
        removed = ollama_proxy.filter_windows_tools(payload)
        self.assertEqual(removed, 2)
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_list_tool_choice_mixed_keeps_non_windows(self):
        """List-form tool_choice (all tools removed) keeps non-Windows entries,
        drops Windows ones — mirroring the dict-form semantics."""
        payload = {
            "tools": [
                {"type": "function", "function": {"name": "powershell"}},
                {"type": "function", "function": {"name": "cmd"}},
            ],
            # Mixed list: powershell/cmd are Windows (dropped), bash is kept.
            "tool_choice": ["powershell", "bash"],
        }
        removed = ollama_proxy.filter_windows_tools(payload)
        self.assertEqual(removed, 2)
        self.assertNotIn("tools", payload)
        # Only the non-Windows entry survives in tool_choice.
        self.assertEqual(payload["tool_choice"], ["bash"])

    def test_no_tools_returns_zero(self):
        payload = {"messages": []}
        self.assertEqual(ollama_proxy.filter_windows_tools(payload), 0)

    def test_empty_tools_list(self):
        payload = {"tools": []}
        self.assertEqual(ollama_proxy.filter_windows_tools(payload), 0)

    def test_non_list_tools(self):
        payload = {"tools": "not-a-list"}  # type: ignore
        self.assertEqual(ollama_proxy.filter_windows_tools(payload), 0)

    def test_no_windows_tools_unchanged(self):
        tools = [
            {"type": "function", "function": {"name": "bash"}},
            {"type": "function", "function": {"name": "python"}},
        ]
        payload = {"tools": tools}
        removed = ollama_proxy.filter_windows_tools(payload)
        self.assertEqual(removed, 0)
        self.assertEqual(payload["tools"], tools)


# ===================================================================
# 3. INTEGRATION — model rewrite & thinking directives
# ===================================================================

class ModelRewriteTests(ProxyIntegrationBase):
    """Verify the proxy rewrites model + injects reasoning_effort."""

    def test_model_rewritten_to_target(self):
        self._start_proxy()
        body = json.dumps({"model": "some-other-model", "messages": [{"role": "user", "content": "hi"}]}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        fwd = self.fake.last_request()
        fwd_payload = json.loads(fwd["body"])
        self.assertEqual(fwd_payload["model"], "test-model")

    def test_nothink_directive_injects_reasoning_effort_none(self):
        self._start_proxy()
        body = json.dumps({"model": "qwen3-coder-nothink", "messages": []}).encode()
        status, _, _ = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        fwd_payload = json.loads(self.fake.last_request()["body"])
        self.assertEqual(fwd_payload["model"], "test-model")
        self.assertEqual(fwd_payload.get("reasoning_effort"), "none")
        self.assertIn("thinking directive from", self.read_proxy_log())

    def test_think_high_directive(self):
        self._start_proxy()
        body = json.dumps({"model": "qwen3-coder-think-high", "messages": []}).encode()
        status, _, _ = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        fwd_payload = json.loads(self.fake.last_request()["body"])
        self.assertEqual(fwd_payload.get("reasoning_effort"), "high")

    def test_default_effort_fallback(self):
        """When model ID has no directive, --default-effort is used."""
        self.extra_args = ["--default-effort", "medium"]
        self._start_proxy()
        body = json.dumps({"model": "plain-model-id", "messages": []}).encode()
        status, _, _ = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        fwd_payload = json.loads(self.fake.last_request()["body"])
        self.assertEqual(fwd_payload.get("reasoning_effort"), "medium")

    def test_no_directive_no_default_no_injection(self):
        """Without directive or --default-effort, no reasoning_effort is added."""
        self._start_proxy()
        body = json.dumps({"model": "plain-model", "messages": []}).encode()
        status, _, _ = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        fwd_payload = json.loads(self.fake.last_request()["body"])
        self.assertNotIn("reasoning_effort", fwd_payload)


# ===================================================================
# 4. INTEGRATION — Windows tool filtering (end-to-end)
# ===================================================================

class WindowsToolFilteringIntegrationTests(ProxyIntegrationBase):
    """Verify --filter-windows-tools strips Windows tools in the forwarded payload."""

    def test_filters_windows_tools_in_chat_completions(self):
        self.extra_args = ["--filter-windows-tools"]
        self._start_proxy()

        body = json.dumps({
            "model": "m",
            "messages": [],
            "tools": [
                {"type": "function", "function": {"name": "powershell"}},
                {"type": "function", "function": {"name": "bash"}},
            ],
        }).encode()
        status, _, _ = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        fwd_payload = json.loads(self.fake.last_request()["body"])
        names = [t["function"]["name"] for t in fwd_payload.get("tools", [])]
        self.assertNotIn("powershell", names)
        self.assertIn("bash", names)
        self.assertIn("filtered 1 Windows tool(s)", self.read_proxy_log())

    def test_non_chat_path_untouched(self):
        """Windows filtering only applies to /v1/chat/completions."""
        self.extra_args = ["--filter-windows-tools"]
        self._start_proxy()

        body = json.dumps({
            "model": "m",
            "tools": [{"type": "function", "function": {"name": "powershell"}}],
        }).encode()
        # POST to a non-chat path — should be forwarded as-is (no filtering)
        status, _, _ = http_post(f"{self.proxy_url}/v1/embeddings", body)
        self.assertEqual(status, 200)

        fwd_payload = json.loads(self.fake.last_request()["body"])
        # Model is still rewritten, but tools are NOT filtered on non-chat paths
        self.assertEqual(fwd_payload["model"], "test-model")
        self.assertIn("tools", fwd_payload)


# ===================================================================
# 5. INTEGRATION — forced-streaming reassembly
# ===================================================================

class ForcedStreamingReassemblyTests(ProxyIntegrationBase):
    """Client sends stream=false; proxy forces stream=true upstream and
    reassembles SSE into a single chat.completion JSON body."""

    def _setup_sse_fake(self, content: str = "Hello world",
                        tool_calls: list | None = None,
                        finish_reason: str = "stop",
                        usage: dict | None = None):
        """Configure fake to return an SSE stream with the given content."""
        chunks = []
        # First chunk: role + content start
        c1 = {"choices": [{"delta": {"role": "assistant", "content": content[:len(content)//2]}}]}
        chunks.append(json.dumps(c1))
        # Second chunk: rest of content
        if len(content) > 1:
            c2 = {"choices": [{"delta": {"content": content[len(content)//2:]}}]}
            chunks.append(json.dumps(c2))
        # Tool calls (if any)
        for i, tc in enumerate(tool_calls or []):
            tc_chunk = {"choices": [{"delta": {"tool_calls": [
                {"index": i, "id": f"call_{i}", "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc.get("args", {}))}}
            ]}}]}
            chunks.append(json.dumps(tc_chunk))
        # Final chunk: finish_reason + usage
        final = {"choices": [{"delta": {}, "finish_reason": finish_reason}]}
        if usage:
            final["usage"] = usage
        chunks.append(json.dumps(final))

        def handler(h, method, path, body, n):
            resp = sse_chunks(*chunks)
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Content-Length", str(len(resp)))
            h.end_headers()
            h.wfile.write(resp)

        self.fake.on_request = handler

    def test_stream_false_reassembled_to_json(self):
        self._setup_sse_fake(content="Hello world")
        self._start_proxy()

        body = json.dumps({
            "model": "m", "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        status, headers, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        # Verify upstream received stream=true (forced)
        fwd_payload = json.loads(self.fake.last_request()["body"])
        self.assertTrue(fwd_payload["stream"], "proxy must force stream=true upstream")

        # Verify response is a single chat.completion JSON
        result = json.loads(resp_body)
        self.assertEqual(result["object"], "chat.completion")
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["choices"][0]["message"]["content"], "Hello world")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertIn("id", result)
        self.assertTrue(result["id"].startswith("chatcmpl-"))

    def test_reassembly_with_tool_calls(self):
        self._setup_sse_fake(
            content="",
            tool_calls=[
                {"name": "bash", "args": {"command": "ls"}},
                {"name": "read_file", "args": {"path": "/tmp/x"}},
            ],
        )
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": False, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        result = json.loads(resp_body)
        tcs = result["choices"][0]["message"].get("tool_calls", [])
        self.assertEqual(len(tcs), 2)
        self.assertEqual(tcs[0]["function"]["name"], "bash")
        self.assertEqual(json.loads(tcs[0]["function"]["arguments"]), {"command": "ls"})
        self.assertEqual(tcs[1]["function"]["name"], "read_file")

    def test_reassembly_with_usage(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        self._setup_sse_fake(content="ok", usage=usage)
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": False, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        result = json.loads(resp_body)
        self.assertEqual(result.get("usage"), usage)


# ===================================================================
# 6. INTEGRATION — streaming passthrough
# ===================================================================

class StreamingPassthroughTests(ProxyIntegrationBase):
    """Client sends stream=true; proxy passes SSE through line-by-line."""

    def test_streaming_passthrough(self):
        chunks = [
            json.dumps({"choices": [{"delta": {"content": "Hel"}}]}),
            json.dumps({"choices": [{"delta": {"content": "lo!"}}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        ]

        def handler(h, method, path, body, n):
            resp = sse_chunks(*chunks)
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            # Write in small pieces to simulate token streaming
            for line in resp.split(b"\n\n"):
                if line:
                    h.wfile.write(line + b"\n\n")
                    h.wfile.flush()

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

        text = resp_body.decode("utf-8")
        self.assertIn("data:", text)
        self.assertIn("[DONE]", text)
        # Content should be present in the SSE chunks
        self.assertIn("Hel", text)
        self.assertIn("lo!", text)


# ===================================================================
# 7. INTEGRATION — keep-alive pings during upstream silence
# ===================================================================

class KeepAlivePingTests(ProxyIntegrationBase):
    """When Ollama is silent (thinking), proxy emits ': keep-alive' SSE comments."""

    def test_keepalive_ping_during_silence(self):
        # Set KEEPALIVE_INTERVAL to 1s so the ping fires quickly
        self.extra_env = {"PROXY_KEEPALIVE_INTERVAL": "1"}

        def handler(h, method, path, body, n):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            # Simulate thinking silence (no bytes sent). Must exceed the
            # proxy's 5.0s queue poll_interval so one full Empty cycle elapses
            # and a keep-alive ping is emitted before data arrives.
            time.sleep(6.5)
            # Then send the actual response
            chunk = json.dumps({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]})
            h.wfile.write(f"data: {chunk}\n\n".encode())
            h.wfile.write(b"data: [DONE]\n\n")
            h.wfile.flush()

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body, timeout=30)
        self.assertEqual(status, 200)

        text = resp_body.decode("utf-8")
        # Must contain at least one keep-alive ping (SSE comment line)
        self.assertIn(": keep-alive", text, f"expected keep-alive ping in: {text!r}")
        # And the actual data
        self.assertIn("done", text)


# ===================================================================
# 8. INTEGRATION — client disconnect mid-stream aborts upstream
# ===================================================================

class ClientDisconnectMidStreamTests(ProxyIntegrationBase):
    """When the client closes mid-stream, proxy aborts the upstream connection."""

    def test_client_disconnect_aborts_upstream(self):
        disconnected = threading.Event()

        def handler(h, method, path, body, n):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            # Stream 5 chunks with delays; detect if client disconnects
            for i in range(5):
                try:
                    chunk = json.dumps({"choices": [{"delta": {"content": f"tok{i}"}}]})
                    h.wfile.write(f"data: {chunk}\n\n".encode())
                    h.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    disconnected.set()
                    return
                time.sleep(0.5)
            # Final chunk
            try:
                h.wfile.write(b"data: [DONE]\n\n")
                h.wfile.flush()
            except Exception:
                disconnected.set()

        self.fake.on_request = handler
        self._start_proxy()

        # Use raw socket so we can close mid-stream
        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        request = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.proxy_port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body

        sock = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=15)
        try:
            sock.sendall(request)
            # Read until we get at least one data chunk
            buf = b""
            while b"data:" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            self.assertIn(b"data:", buf, "should have received at least one SSE chunk")
        finally:
            # Abruptly close — simulates client abort
            sock.close()

        # Give the proxy time to detect and log
        time.sleep(1.5)
        log = self.read_proxy_log()
        # One of these markers should appear (depending on where disconnect was detected)
        self.assertTrue(
            "client disconnected mid-stream" in log
            or "client gone during keep-alive" in log
            or "aborting upstream to free the Ollama slot" in log,
            f"expected disconnect-abort marker in log:\n{log}"
        )


# ===================================================================
# 9. INTEGRATION — patient retry on 503/429 (header phase)
# ===================================================================

class PatientRetryTests(ProxyIntegrationBase):
    """Ollama returns 503 (slot busy); proxy retries patiently until success."""

    def test_flaky_503_then_success(self):
        # First 2 requests → 503; third → 200 SSE
        self.extra_env = {
            "PROXY_BUSY_RETRY_BUDGET": "10",
            "PROXY_BUSY_RETRY_INTERVAL": "1",
        }

        def handler(h, method, path, body, n):
            if n <= 2:
                resp = b'{"error":"slot busy"}'
                h.send_response(503)
                h.send_header("Content-Type", "application/json")
                h.send_header("Content-Length", str(len(resp)))
                h.end_headers()
                h.wfile.write(resp)
            else:
                chunk = json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
                resp = sse_chunks(chunk)
                h.send_response(200)
                h.send_header("Content-Type", "text/event-stream")
                h.send_header("Content-Length", str(len(resp)))
                h.end_headers()
                h.wfile.write(resp)

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        t0 = time.monotonic()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body, timeout=30)
        elapsed = time.monotonic() - t0

        self.assertEqual(status, 200)
        self.assertIn("ok", resp_body.decode())
        # Should have waited at least one retry interval (~1s)
        self.assertGreaterEqual(elapsed, 0.8)
        log = self.read_proxy_log()
        self.assertIn("slot busy; patient retry", log)
        self.assertIn("patient retry succeeded", log)

    def test_client_leaves_during_patient_retry(self):
        """Client abandons the request while proxy is in patient-retry loop.
        Proxy should stop waiting WITHOUT aborting other upstreams."""
        self.extra_env = {
            "PROXY_BUSY_RETRY_BUDGET": "30",  # long budget so it would keep retrying
            "PROXY_BUSY_RETRY_INTERVAL": "1",
        }

        def handler(h, method, path, body, n):
            resp = b'{"error":"busy"}'
            h.send_response(503)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(resp)))
            h.end_headers()
            h.wfile.write(resp)

        self.fake.on_request = handler
        self._start_proxy()

        # Send request via raw socket, then close after a short delay
        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        request = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.proxy_port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body

        sock = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=15)
        try:
            sock.sendall(request)
            time.sleep(2.0)  # let proxy enter patient-retry loop, then leave
        finally:
            sock.close()

        time.sleep(2.0)  # give proxy time to detect client departure
        log = self.read_proxy_log()
        self.assertIn("client left during patient retry", log)
        # Must NOT have aborted other upstreams (no "aborted N upstream" for this reason)
        self.assertNotIn("aborted 1 upstream connection(s): client left", log)


# ===================================================================
# 10. INTEGRATION — MAX_QUEUE_WAIT deadline
# ===================================================================

class QueueWaitDeadlineTests(ProxyIntegrationBase):
    """Fake Ollama accepts the connection but never sends headers within budget."""

    def test_queue_wait_deadline_returns_503(self):
        self.extra_env = {"PROXY_MAX_QUEUE_WAIT": "2"}  # 2s deadline

        def handler(h, method, path, body, n):
            # Accept the connection (read the request) but never respond.
            # Sleep past the proxy's queue-wait deadline.
            time.sleep(10)
            # If we get here, the proxy has already given up.
            try:
                h.send_response(200)
                h.end_headers()
            except Exception:
                pass

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        t0 = time.monotonic()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body, timeout=30)
        elapsed = time.monotonic() - t0

        self.assertEqual(status, 503)
        result = json.loads(resp_body)
        msg = result.get("error", {}).get("message", "")
        self.assertIn("upstream did not respond within", msg)
        self.assertIn("steer or /_abort", msg)
        # Should have taken ~2s (the deadline), not 10s
        self.assertLess(elapsed, 8.0)
        log = self.read_proxy_log()
        self.assertIn("upstream did not respond within", log)

    def test_client_gone_during_queue_wait(self):
        """Client disconnects before the queue-wait deadline → slot abandoned."""
        self.extra_env = {"PROXY_MAX_QUEUE_WAIT": "30"}  # long deadline

        def handler(h, method, path, body, n):
            time.sleep(15)  # never respond in time
            try:
                h.send_response(200)
                h.end_headers()
            except Exception:
                pass

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        request = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.proxy_port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body

        sock = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=15)
        try:
            sock.sendall(request)
            time.sleep(1.0)  # wait a bit, then leave
        finally:
            sock.close()

        time.sleep(2.0)
        log = self.read_proxy_log()
        self.assertIn("client disconnected during queue wait", log)


# ===================================================================
# 11. INTEGRATION — kill-and-respawn (worker dies before any data)
# ===================================================================

class KillAndRespawnTests(ProxyIntegrationBase):
    """First attempt: Ollama RSTs after headers (reader worker dies).
    Proxy must re-issue and succeed on the second attempt."""

    def test_respawn_after_worker_death(self):
        self.extra_env = {"PROXY_UPSTREAM_RETRIES": "2"}  # allow 2 attempts

        def handler(h, method, path, body, n):
            if n == 1:
                # Send headers, then RST-close (reader will get ConnectionResetError)
                h.send_response(200)
                h.send_header("Content-Type", "text/event-stream")
                h.end_headers()
                self.fake.rst_close(h)
            else:
                # Second attempt: normal SSE response
                chunk = json.dumps({"choices": [{"delta": {"content": "recovered"}, "finish_reason": "stop"}]})
                resp = sse_chunks(chunk)
                h.send_response(200)
                h.send_header("Content-Type", "text/event-stream")
                h.send_header("Content-Length", str(len(resp)))
                h.end_headers()
                h.wfile.write(resp)

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": False, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body, timeout=30)
        self.assertEqual(status, 200)

        result = json.loads(resp_body)
        self.assertEqual(result["choices"][0]["message"]["content"], "recovered")

        log = self.read_proxy_log()
        self.assertIn("worker died before any data", log)
        self.assertIn("respawning", log)


# ===================================================================
# 12. INTEGRATION — auto-recovery fail-streak + /_health
# ===================================================================

class AutoRecoveryTests(ProxyIntegrationBase):
    """Consecutive upstream failures trigger abort-all; success resets streak."""

    def test_fail_streak_triggers_recovery(self):
        # Each request → 503, no retries (retries=1), no patient retry (budget=0)
        self.extra_env = {
            "PROXY_UPSTREAM_RETRIES": "1",
            "PROXY_BUSY_RETRY_BUDGET": "0",
            "PROXY_RECOVERY_FAILS": "2",  # low threshold for fast test
        }

        def handler(h, method, path, body, n):
            resp = b'{"error":"busy"}'
            h.send_response(503)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(resp)))
            h.end_headers()
            h.wfile.write(resp)

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        # Fire 3 requests (threshold=2, so 3rd triggers recovery)
        for _ in range(3):
            status, _, _ = http_post(f"{self.proxy_url}/v1/chat/completions", body, timeout=10)
            self.assertEqual(status, 502)  # proxy surfaces 502 after retries exhausted

        log = self.read_proxy_log()
        self.assertIn("consecutive upstream failures", log)
        self.assertIn("aborting in-flight upstreams", log)

    def test_health_endpoint_shape(self):
        self._start_proxy()
        status, _, resp_body = http_get(f"{self.proxy_url}/_health")
        self.assertEqual(status, 200)
        result = json.loads(resp_body)
        self.assertEqual(result["status"], "ok")
        self.assertIn("active_upstreams", result)
        self.assertIn("fail_streak", result)
        self.assertIn("recovery_threshold", result)
        self.assertIsInstance(result["active_upstreams"], int)
        self.assertIsInstance(result["fail_streak"], int)

    def test_health_works_even_when_upstream_failing(self):
        """/_health must not touch Ollama — works even after failures."""
        self.extra_env = {"PROXY_UPSTREAM_RETRIES": "1", "PROXY_BUSY_RETRY_BUDGET": "0"}

        def handler(h, method, path, body, n):
            resp = b'{"error":"busy"}'
            h.send_response(503)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(resp)))
            h.end_headers()
            h.wfile.write(resp)

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        http_post(f"{self.proxy_url}/v1/chat/completions", body, timeout=10)  # fail once

        # /_health still works
        status, _, resp_body = http_get(f"{self.proxy_url}/_health")
        self.assertEqual(status, 200)
        result = json.loads(resp_body)
        self.assertEqual(result["status"], "ok")


# ===================================================================
# 13. INTEGRATION — /_abort control endpoint
# ===================================================================

class AbortEndpointTests(ProxyIntegrationBase):
    """POST /_abort closes all in-flight upstream connections."""

    def test_abort_returns_count(self):
        # Start a long streaming request, then abort it
        def handler(h, method, path, body, n):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            # Stream slowly so the request stays in-flight
            for i in range(20):
                try:
                    chunk = json.dumps({"choices": [{"delta": {"content": f"t{i}"}}]})
                    h.wfile.write(f"data: {chunk}\n\n".encode())
                    h.wfile.flush()
                except Exception:
                    return
                time.sleep(0.3)

        self.fake.on_request = handler
        self._start_proxy()

        # Start a streaming request in a background thread (it will be slow)
        result_holder = {}

        def make_request():
            body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
            try:
                s, _, b = http_post(f"{self.proxy_url}/v1/chat/completions", body, timeout=30)
                result_holder["status"] = s
            except Exception as e:
                result_holder["error"] = str(e)

        t = threading.Thread(target=make_request, daemon=True)
        t.start()
        time.sleep(1.0)  # let it get in-flight

        # Now abort
        status, _, resp_body = http_post(f"{self.proxy_url}/_abort", b"{}", timeout=5)
        self.assertEqual(status, 200)
        result = json.loads(resp_body)
        self.assertGreaterEqual(result.get("aborted", 0), 1)

        t.join(timeout=5)


# ===================================================================
# 14. INTEGRATION — error handling edge cases
# ===================================================================

class ErrorHandlingTests(ProxyIntegrationBase):

    def test_chunked_transfer_encoding_rejected(self):
        self._start_proxy()
        # Send a request with Transfer-Encoding: chunked (no Content-Length)
        request = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.proxy_port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Transfer-Encoding: chunked\r\n"
            f"\r\n"
        ).encode()
        status, body = raw_http_request("127.0.0.1", self.proxy_port, request)
        self.assertEqual(status, 411)
        self.assertIn(b"chunked", body.lower())

    def test_invalid_content_length_rejected(self):
        self._start_proxy()
        request = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.proxy_port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: not-a-number\r\n"
            f"\r\n"
        ).encode()
        status, body = raw_http_request("127.0.0.1", self.proxy_port, request)
        self.assertEqual(status, 400)
        self.assertIn(b"Content-Length", body)

    def test_permanent_4xx_forwarded_with_real_status(self):
        """Upstream 400 (e.g., invalid model) is forwarded as-is, not masked as 502."""
        def handler(h, method, path, body, n):
            resp = b'{"error":"model not found"}'
            h.send_response(400)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(resp)))
            h.end_headers()
            h.wfile.write(resp)

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 400)
        result = json.loads(resp_body)
        self.assertIn("model not found", result.get("error", ""))

    def test_non_json_body_forwarded_untouched(self):
        """Non-JSON POST bodies are forwarded without JSON parsing/re-serialization.

        The proxy forwards non-forced-stream responses line-by-line and
        normalizes each line to end in a newline (SSE transport contract), so
        we use a newline-terminated payload — the realistic case for real
        Ollama responses — and assert it passes through byte-for-byte.
        """
        raw_body = b"\x00\x01\x02 binary-data-not-json\n"

        def handler(h, method, path, body, n):
            # Echo back what we received to verify it's untouched
            h.send_response(200)
            h.send_header("Content-Type", "application/octet-stream")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)

        self.fake.on_request = handler
        self._start_proxy()

        # POST raw binary to a non-chat path (avoids JSON parsing branch)
        status, _, resp_body = http_post(
            f"{self.proxy_url}/v1/embeddings", raw_body,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(status, 200)
        # The fake echoes the body back — verify it's identical
        self.assertEqual(resp_body, raw_body)

    def test_get_passthrough(self):
        """GET requests (e.g., /api/tags) pass through to Ollama."""
        tags_response = b'{"models":[{"name":"qwen3-coder:latest"}]}'

        def handler(h, method, path, body, n):
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(tags_response)))
            h.end_headers()
            h.wfile.write(tags_response)

        self.fake.on_request = handler
        self._start_proxy()

        status, _, resp_body = http_get(f"{self.proxy_url}/api/tags")
        self.assertEqual(status, 200)
        result = json.loads(resp_body)
        self.assertIn("models", result)


# ===================================================================
# 15. INTEGRATION — log timestamp format
# ===================================================================

class LogTimestampTests(ProxyIntegrationBase):
    """Verify all proxy log lines carry a YYYY-MM-DD HH:MM:SS prefix."""

    def test_log_lines_are_timestamped(self):
        self._start_proxy()
        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        http_post(f"{self.proxy_url}/v1/chat/completions", body)

        log = self.read_proxy_log()
        # Find [proxy] lines and verify they have a timestamp prefix
        proxy_lines = [l for l in log.splitlines() if "[proxy]" in l]
        self.assertTrue(proxy_lines, "expected at least one [proxy] log line")
        import re
        ts_pattern = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[proxy\]")
        for line in proxy_lines:
            self.assertRegex(
                line, ts_pattern,
                f"log line missing timestamp prefix: {line!r}"
            )


# ===================================================================
# 16. INTEGRATION — BrokenPipeError suppression on client disconnect
# ===================================================================

class BrokenPipeSuppressionTests(ProxyIntegrationBase):
    """When the client disconnects mid-stream, the proxy must NOT dump a
    BrokenPipeError traceback into the log. The disconnect is already logged
    with a clean message; the finish() override swallows the socketserver
    flush/close error."""

    def test_no_brokenpipe_traceback_on_disconnect(self):
        """Client closes mid-stream → no 'BrokenPipeError' or 'Traceback' in log."""

        def handler(h, method, path, body, n):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            # Stream slowly so the client can disconnect mid-way
            for i in range(10):
                try:
                    chunk = json.dumps({"choices": [{"delta": {"content": f"tok{i}"}}]})
                    h.wfile.write(f"data: {chunk}\n\n".encode())
                    h.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(0.4)

        self.fake.on_request = handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        request = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.proxy_port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body

        sock = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=15)
        try:
            sock.sendall(request)
            # Read one chunk, then abruptly close (simulates VS Code abort)
            buf = b""
            while b"data:" not in buf:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
        finally:
            sock.close()

        # Give the proxy time to detect disconnect, finish(), and log
        time.sleep(2.0)
        log = self.read_proxy_log()

        # The clean disconnect message should be present
        self.assertTrue(
            "client disconnected mid-stream" in log
            or "client gone" in log
            or "aborting upstream" in log,
            f"expected a clean disconnect log message:\n{log}"
        )
        # Critically: NO BrokenPipeError traceback should appear
        self.assertNotIn("BrokenPipeError", log,
                         f"BrokenPipeError traceback leaked into log:\n{log}")
        self.assertNotIn("Traceback (most recent call last)", log,
                         f"unhandled exception traceback in log:\n{log}")

    def test_no_brokenpipe_on_forced_stream_disconnect(self):
        """Same check for the forced-stream (non-streaming client) path."""

        def handler(h, method, path, body, n):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            # Slow SSE so the client can leave before reassembly completes
            for i in range(10):
                try:
                    chunk = json.dumps({"choices": [{"delta": {"content": f"t{i}"}}]})
                    h.wfile.write(f"data: {chunk}\n\n".encode())
                    h.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(0.4)

        self.fake.on_request = handler
        self._start_proxy()

        # Non-streaming request → proxy forces stream=true upstream
        body = json.dumps({"model": "m", "stream": False, "messages": []}).encode()
        request = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.proxy_port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body

        sock = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=15)
        try:
            sock.sendall(request)
            time.sleep(1.0)  # let it start streaming upstream, then leave
        finally:
            sock.close()

        time.sleep(2.0)
        log = self.read_proxy_log()
        self.assertNotIn("BrokenPipeError", log,
                         f"BrokenPipeError traceback leaked into log:\n{log}")
        self.assertNotIn("Traceback (most recent call last)", log,
                         f"unhandled exception traceback in log:\n{log}")


# ===================================================================
# 17. INTEGRATION — TLS bytes detection on plain-HTTP port
# ===================================================================

class TlsDetectionTests(ProxyIntegrationBase):
    """When a client sends TLS/HTTPS bytes to our plain-HTTP proxy, the proxy
    must detect it, return a clean 426 response, and NOT log binary garbage."""

    def test_tls_handshake_gets_426(self):
        """Send a minimal TLS ClientHello → expect 426 Upgrade Required."""
        self._start_proxy()

        # Minimal TLS 1.2/1.3 ClientHello record:
        #   0x16 = handshake, 0x03 0x01 = TLS 1.0 version (record layer)
        #   followed by length and a few bytes of payload
        tls_bytes = b"\x16\x03\x01\x00\x05\x01\x00\x00\x02\x01\x00"

        sock = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5)
        try:
            sock.sendall(tls_bytes)
            # Read the response
            chunks = []
            sock.settimeout(5)
            while True:
                try:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                except socket.timeout:
                    break
            raw = b"".join(chunks)
        finally:
            sock.close()

        # Must get a 426 status
        self.assertIn(b"426", raw.split(b"\r\n")[0],
                      f"expected 426 in status line, got: {raw[:100]!r}")
        # Response body should be human-readable (not binary garbage)
        header_end = raw.find(b"\r\n\r\n")
        if header_end != -1:
            body = raw[header_end + 4:]
            self.assertIn(b"plain HTTP", body,
                          f"expected helpful message in body, got: {body!r}")

        # Log must contain the clean TLS-detection message
        log = self.read_proxy_log()
        self.assertIn("received TLS bytes on plain-HTTP port", log,
                      f"expected TLS detection log line:\n{log}")
        # And critically: NO binary garbage in the log
        self.assertNotIn(b"\x16\x03\x01".decode("latin-1"), log)

    def test_tls_app_data_gets_426(self):
        """TLS application-data record (0x17 first byte) also detected."""
        self._start_proxy()

        tls_bytes = b"\x17\x03\x03\x00\x05\x01\x02\x03\x04\x05"

        sock = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5)
        try:
            sock.sendall(tls_bytes)
            chunks = []
            sock.settimeout(5)
            while True:
                try:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                except socket.timeout:
                    break
            raw = b"".join(chunks)
        finally:
            sock.close()

        self.assertIn(b"426", raw.split(b"\r\n")[0],
                      f"expected 426, got: {raw[:100]!r}")
        log = self.read_proxy_log()
        self.assertIn("received TLS bytes on plain-HTTP port", log)

    def test_normal_http_still_works_after_tls_detection(self):
        """After a TLS rejection, the proxy must still serve normal HTTP requests."""
        self._start_proxy()

        # First: send TLS garbage (should get 426)
        tls_bytes = b"\x16\x03\x01\x00\x05\x01\x00\x00\x02\x01\x00"
        sock = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5)
        try:
            sock.sendall(tls_bytes)
            sock.recv(4096)  # drain response
        finally:
            sock.close()

        # Second: normal HTTP request must still work
        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)

    def test_non_tls_binary_not_misdetected(self):
        """A normal HTTP request starting with 'P' (POST) must NOT be treated as TLS."""
        self._start_proxy()

        # 'P' = 0x50, not in (0x16, 0x17) — should pass through normally
        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        status, _, _ = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 200)
        # No TLS-detection log line should appear
        log = self.read_proxy_log()
        self.assertNotIn("received TLS bytes", log)


# ===================================================================
# 16. REVIEW FIXES — header forwarding & error-body fidelity
# ===================================================================

class HeaderForwardingTests(ProxyIntegrationBase):
    """The proxy inspects/reassembles the upstream body as text, so it must force
    identity encoding toward Ollama (never forward a client's Accept-Encoding),
    while still forwarding other benign client headers."""

    def _simple_ok_handler(self, h, method, path, body, n):
        resp = b'{"ok":true}'
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(resp)))
        h.end_headers()
        h.wfile.write(resp)

    def test_accept_encoding_forced_identity_upstream(self):
        """A client's Accept-Encoding (e.g. gzip) must NOT reach Ollama — a
        compressed response would be read as opaque bytes and silently break SSE
        reassembly. The proxy forces identity encoding upstream instead."""
        self.fake.on_request = self._simple_ok_handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        http_post(f"{self.proxy_url}/v1/chat/completions", body,
                  headers={"Accept-Encoding": "gzip"})

        fwd = {k.lower(): v for k, v in self.fake.last_request()["headers"].items()}
        # The client asked for gzip; upstream must be told identity instead.
        self.assertEqual(fwd.get("accept-encoding"), "identity")

    def test_other_client_headers_still_forwarded(self):
        """Guard: forcing identity encoding must not over-strip — a benign custom
        header still reaches Ollama unchanged."""
        self.fake.on_request = self._simple_ok_handler
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        http_post(f"{self.proxy_url}/v1/chat/completions", body,
                  headers={"X-Custom-Header": "keepme"})

        fwd = {k.lower(): v for k, v in self.fake.last_request()["headers"].items()}
        self.assertEqual(fwd.get("x-custom-header"), "keepme")


class ErrorForwardingFidelityTests(ProxyIntegrationBase):
    """Regression guard for the LIVE error-forwarding path: a permanent 4xx from
    Ollama is surfaced with its real status and FULL body (urllib raises
    HTTPError; _forward_with_retry_inner forwards e.read()). Multi-line JSON must
    stay valid — not truncated to one line."""

    def _plain_error_handler(self, status: int, raw_body: bytes):
        def handler(h, method, path, body, n):
            h.send_response(status)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(raw_body)))
            h.end_headers()
            h.wfile.write(raw_body)
        self.fake.on_request = handler

    def test_single_line_error_surfaced_intact(self):
        """A single-line 400 error body is forwarded with its real status and
        valid JSON."""
        raw = b'{"error":{"message":"model not found"}}\n'
        self._plain_error_handler(400, raw)
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": False, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 400)
        result = json.loads(resp_body)
        self.assertIn("model not found", result["error"]["message"])

    def test_multiline_error_surfaced_intact(self):
        """A multi-line 400 error body is forwarded in full (valid JSON), proving
        the live path preserves the whole body rather than a single line."""
        raw = (b'{\n'
               b'  "error": {\n'
               b'    "message": "context length exceeded",\n'
               b'    "type": "invalid_request_error"\n'
               b'  }\n'
               b'}\n')
        self._plain_error_handler(400, raw)
        self._start_proxy()

        body = json.dumps({"model": "m", "stream": False, "messages": []}).encode()
        status, _, resp_body = http_post(f"{self.proxy_url}/v1/chat/completions", body)
        self.assertEqual(status, 400)
        result = json.loads(resp_body)   # must be valid JSON (full body preserved)
        self.assertIn("context length exceeded", result["error"]["message"])


# ===================================================================
# Runner
# ===================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
