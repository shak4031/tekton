"""A scriptable stand-in for Hermes ``:8787``.

Exists so slice 2 is testable from the rig with the Pi powered off, and so the
failure paths that matter (401, 404, 409, empty queue, malformed body) can be
provoked on demand instead of waited for. Deterministic, no LLM, seconds --
the same shape as the D3.2 escape tests.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_SECRET = "stub-runner-secret"  # noqa: S105 - test fixture, not a credential

_JOB = {
    "id": "lifepilot-a1b2",
    "project": "lifepilot",
    "description": "add a docstring to store.claim",
    "state": "planning",
    "worker": "tekton-rig",
    "attempts": 1,
}


class StubHermes:
    """An in-process Hermes whose responses each test scripts for itself."""

    def __init__(self, secret: str = DEFAULT_SECRET) -> None:
        """Start the stub on an ephemeral port with the default route table."""
        self.secret = secret
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[str, tuple[int, Any]] = {
            "/jobs/claim": (200, dict(_JOB)),
            "/jobs/lifepilot-a1b2/state": (200, {"ok": True}),
            "/jobs/lifepilot-a1b2/result": (200, {"ok": True}),
            "/jobs/lifepilot-a1b2/heartbeat": (200, {"ok": True}),
        }
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """Base URL the client should be pointed at."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def script(self, path: str, status: int, payload: Any = None) -> None:
        """Make ``path`` answer with ``status`` and ``payload`` from now on."""
        self.routes[path] = (status, payload)

    def last(self) -> dict[str, Any]:
        """Return the most recent recorded request."""
        return self.requests[-1]

    def stop(self) -> None:
        """Shut the stub down and join its thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _make_handler(stub: StubHermes) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to ``stub``'s route table and log."""

    class Handler(BaseHTTPRequestHandler):
        """Records every POST, checks the shared secret, replays the script."""

        protocol_version = "HTTP/1.1"

        def log_message(self, _fmt: str, *_args: Any) -> None:
            """Silence the default stderr access log."""

        def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            """Handle the four queue endpoints."""
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            stub.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(raw) if raw else None,
                }
            )
            if self.headers.get("X-Hermes-Secret") != stub.secret:
                self._reply(401, {"error": "bad secret"})
                return
            status, payload = stub.routes.get(self.path, (404, {"error": "no route"}))
            self._reply(status, payload)

        def _reply(self, status: int, payload: Any) -> None:
            """Write a status line, headers and an optional JSON body."""
            if payload is None:
                body = b""
            elif isinstance(payload, bytes):
                body = payload  # raw escape hatch: send bytes that are not valid JSON
            else:
                body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

    return Handler
