"""A local stand-in for `api.retina.fm`, for developing and testing stage 3.

Run it, point the service at it:

    python tools/mock_server.py --port 8080
    curl -s localhost:8080/v1/nodes/register -d '{...}'

Or start it inside a test, on a random port, with no process management:

    with MockServer() as server:
        ...                       # server.url is http://127.0.0.1:<port>/v1
        assert server.requests[-1].path == "/v1/nodes/detection"

One implementation, two uses. Standard library only — four endpoints do not
justify a web framework, and this must not add a runtime dependency.

## Why not Prism

Generating a mock from the OpenAPI document gives correctly-shaped replies for
free, but it always cooperates. Everything worth testing in stage 3 is about
the server *refusing*: a revoked token must stop the stream without triggering
re-registration, a `409` must force a config resend and then resume, a
`Retry-After` must actually be waited out. None of that is reachable from a
server that only says yes, so the control channel below is the point of this
file rather than an extra.

## Fidelity

Requests are validated against the same generated models the client builds
them with, so a malformed payload is rejected here the way the real server
would reject it — including the constraints the spec carries, like
``node_id``'s pattern and ``config_version``'s minimum.

It is deliberately *not* a simulator. Registration rate limits, the reflash
window and the Mender acceptance sweep are all server-side concerns the node
cannot observe except as an opaque `403`, so they are scripted rather than
modelled.
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pydantic

from retina_telemetry.wire.models import (
    DetectionFrame,
    HeartbeatRequest,
    NodeConfig,
    RegisterRequest,
)

BASE_PATH = "/v1"

#: Endpoint name → (method, path, request model). The name is what the control
#: channel uses to queue a scripted response.
ENDPOINTS = {
    "register": ("POST", f"{BASE_PATH}/nodes/register", RegisterRequest),
    "detection": ("POST", f"{BASE_PATH}/nodes/detection", DetectionFrame),
    "heartbeat": ("POST", f"{BASE_PATH}/nodes/heartbeat", HeartbeatRequest),
    "config": ("PUT", f"{BASE_PATH}/nodes/config", NodeConfig),
}


@dataclass
class RecordedRequest:
    """What the server saw, for a test to assert against."""

    method: str
    path: str
    endpoint: str | None
    body: Any
    authorization: str | None
    at: datetime

    @property
    def bearer(self) -> str | None:
        if self.authorization and self.authorization.startswith("Bearer "):
            return self.authorization.removeprefix("Bearer ")
        return None


@dataclass
class ScriptedResponse:
    """A reply queued by the control channel, consumed once."""

    status: int
    body: dict[str, Any] | None = None
    retry_after: int | None = None


@dataclass
class State:
    """Everything the mock knows. Guarded by :attr:`lock`."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    requests: list[RecordedRequest] = field(default_factory=list)
    #: TCP connections accepted, not requests served. The client is meant to
    #: hold one open across every request, so this is how keep-alive is proved.
    connections: int = 0
    scripted: dict[str, list[ScriptedResponse]] = field(default_factory=dict)

    token: str | None = None
    node_ref: str = "nd4f2k9xq7m3b8vc"
    config_version: int = 1
    #: The configuration behind the active version, so a resend of something
    #: identical does not create a new one.
    active_config: dict[str, Any] | None = None

    # The levels every response restates. The spec makes these levels rather
    # than edges precisely so a node that missed one still learns.
    config_stale: bool = False
    streaming_allowed: bool = True

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.connections = 0
            self.scripted.clear()
            self.token = None
            self.config_version = 1
            self.active_config = None
            self.config_stale = False
            self.streaming_allowed = True


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so keep-alive works: the client is meant to hold one connection
    # open across every request, and a mock that closed each time would hide
    # any bug in that.
    protocol_version = "HTTP/1.1"

    state: State  # injected by MockServer

    def setup(self) -> None:
        # Called once per accepted connection rather than per request.
        super().setup()
        with self.state.lock:
            self.state.connections += 1

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ── plumbing ─────────────────────────────────────────────────────

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    def _send(
        self, status: int, body: dict[str, Any] | None = None, retry_after: int | None = None
    ) -> None:
        encoded = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: int, error: str, detail: str | None = None, retry_after=None) -> None:
        body: dict[str, Any] = {"error": error}
        if detail:
            body["detail"] = detail
        self._send(status, body, retry_after)

    def _endpoint_for(self, method: str, path: str) -> str | None:
        for name, (m, p, _) in ENDPOINTS.items():
            if m == method and p == path:
                return name
        return None

    # ── dispatch ─────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path == "/_control/requests":
            with self.state.lock:
                self._send(
                    200,
                    {
                        "requests": [
                            {
                                "method": r.method,
                                "path": r.path,
                                "endpoint": r.endpoint,
                                "bearer": r.bearer,
                                "body": r.body,
                                "at": r.at.isoformat(),
                            }
                            for r in self.state.requests
                        ]
                    },
                )
            return
        if self.path == "/_control/state":
            with self.state.lock:
                self._send(
                    200,
                    {
                        "token_issued": self.state.token is not None,
                        "config_version": self.state.config_version,
                        "config_stale": self.state.config_stale,
                        "streaming_allowed": self.state.streaming_allowed,
                        "node_ref": self.state.node_ref,
                        "connections": self.state.connections,
                        "scripted": {k: len(v) for k, v in self.state.scripted.items()},
                    },
                )
            return
        self._error(404, "not_found")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/_control/"):
            self._control()
            return
        self._handle("POST")

    # ── control channel ──────────────────────────────────────────────

    def _control(self) -> None:
        body = self._read_body() or {}

        if self.path == "/_control/reset":
            self.state.reset()
            self._send(200, {"ok": True})
            return

        if self.path == "/_control/enqueue":
            endpoint = body.get("endpoint")
            if endpoint not in ENDPOINTS:
                self._error(400, "unknown_endpoint", f"expected one of {sorted(ENDPOINTS)}")
                return
            queued = ScriptedResponse(
                status=int(body["status"]),
                body=body.get("body"),
                retry_after=body.get("retry_after"),
            )
            with self.state.lock:
                self.state.scripted.setdefault(endpoint, []).extend(
                    [queued] * int(body.get("count", 1))
                )
            self._send(200, {"ok": True})
            return

        if self.path == "/_control/levels":
            with self.state.lock:
                for name in ("config_stale", "streaming_allowed"):
                    if name in body:
                        setattr(self.state, name, bool(body[name]))
                if "config_version" in body:
                    self.state.config_version = int(body["config_version"])
                if "node_ref" in body:
                    self.state.node_ref = str(body["node_ref"])
            self._send(200, {"ok": True})
            return

        self._error(404, "not_found")

    # ── the four endpoints ───────────────────────────────────────────

    def _handle(self, method: str) -> None:
        endpoint = self._endpoint_for(method, self.path)
        body = self._read_body()

        with self.state.lock:
            self.state.requests.append(
                RecordedRequest(
                    method=method,
                    path=self.path,
                    endpoint=endpoint,
                    body=body,
                    authorization=self.headers.get("Authorization"),
                    at=datetime.now(UTC),
                )
            )
            scripted = self.state.scripted.get(endpoint or "", [])
            queued = scripted.pop(0) if scripted else None

        if endpoint is None:
            self._error(404, "not_found")
            return

        # A scripted reply short-circuits everything, including auth — the
        # point is to reproduce what the server does, not to justify it.
        if queued is not None:
            self._send(queued.status, queued.body, queued.retry_after)
            return

        if endpoint != "register" and not self._authorised():
            self._error(401, "unauthorized", "bad, revoked or expired token")
            return

        _, _, model = ENDPOINTS[endpoint]
        try:
            model.model_validate(body)
        except pydantic.ValidationError as exc:
            self._error(400, "invalid_request", exc.errors()[0]["msg"])
            return

        getattr(self, f"_{endpoint}")(body)

    def _authorised(self) -> bool:
        with self.state.lock:
            expected = self.state.token
        if expected is None:
            return False
        header = self.headers.get("Authorization") or ""
        return header == f"Bearer {expected}"

    def _register(self, body: dict[str, Any]) -> None:
        with self.state.lock:
            self.state.token = f"tok_{secrets.token_urlsafe(24)}"
            self._send(
                200,
                {
                    "token": self.state.token,
                    "node_ref": self.state.node_ref,
                    "config_version": self.state.config_version,
                    "server_time": _now(),
                },
            )

    def _detection(self, body: dict[str, Any]) -> None:
        with self.state.lock:
            if body.get("config_version") != self.state.config_version:
                self._error(409, "unknown_config_version", "PUT /nodes/config, then resume")
                return
            self._send(
                202,
                {
                    "accepted": len(body.get("delay") or []),
                    "config_stale": self.state.config_stale,
                    "streaming_allowed": self.state.streaming_allowed,
                },
            )

    def _heartbeat(self, body: dict[str, Any]) -> None:
        with self.state.lock:
            self._send(
                200,
                {
                    "server_time": _now(),
                    "config_stale": self.state.config_stale,
                    "streaming_allowed": self.state.streaming_allowed,
                    "node_ref": self.state.node_ref,
                },
            )

    def _config(self, body: dict[str, Any]) -> None:
        with self.state.lock:
            # A new version only when the configuration differs from the active
            # one; the active version is returned either way. Getting this wrong
            # matters: a mock that bumps on every PUT makes a node look like it
            # is churning versions when a real server would leave it alone, and
            # hides any bug that only appears when the version does *not* move.
            if body != self.state.active_config:
                self.state.config_version += 1
                self.state.active_config = body
            self.state.config_stale = False
            self._send(200, {"config_version": self.state.config_version})


class MockServer:
    """The mock, as a context manager. Binds port 0 unless told otherwise."""

    def __init__(self, port: int = 0, verbose: bool = False) -> None:
        self.state = State()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._httpd.verbose = verbose  # type: ignore[attr-defined]
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        """Base URL including the version prefix, as the spec's server block has it."""
        return f"http://127.0.0.1:{self.port}{BASE_PATH}"

    @property
    def connections(self) -> int:
        """TCP connections accepted. One, if the client keeps its alive."""
        with self.state.lock:
            return self.state.connections

    @property
    def requests(self) -> list[RecordedRequest]:
        with self.state.lock:
            return list(self.state.requests)

    def received(self, endpoint: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.endpoint == endpoint]

    def enqueue(
        self,
        endpoint: str,
        status: int,
        *,
        body: dict[str, Any] | None = None,
        retry_after: int | None = None,
        count: int = 1,
    ) -> None:
        """Queue a scripted reply, consumed once per request."""
        if endpoint not in ENDPOINTS:
            raise ValueError(f"unknown endpoint {endpoint!r}, expected one of {sorted(ENDPOINTS)}")
        with self.state.lock:
            self.state.scripted.setdefault(endpoint, []).extend(
                [ScriptedResponse(status, body, retry_after)] * count
            )

    def start(self) -> MockServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)

    def __enter__(self) -> MockServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--quiet", action="store_true", help="suppress the request log")
    args = parser.parse_args()

    server = MockServer(port=args.port, verbose=not args.quiet)
    print(f"mock ingest on {server.url}")
    print("  control:  POST /_control/reset | /_control/enqueue | /_control/levels")
    print("            GET  /_control/requests | /_control/state")
    try:
        server.start()
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()
