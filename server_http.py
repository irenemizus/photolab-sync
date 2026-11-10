"""The SyncServer's stdlib HTTP plumbing.

Wraps the endpoint handlers (server_endpoints.py) in a
http.server.BaseHTTPRequestHandler: parses the JSON request body, dispatches
on (method, path), maps ApiError and ImmichError to the spec's error
responses (design-api.md §4) and logs every call to the server's request log.
"""

import json
import logging
from http.server import BaseHTTPRequestHandler

from immich_client import ImmichError
from sync_api import ApiError

log = logging.getLogger("photolab-sync.server")


def make_handler(server):
    """Build the request handler class, bound to a running PhotolabSyncServer."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence per-request stderr noise
            log.debug("%s - " + fmt, self.address_string(), *args)

        # -- helpers ---------------------------------------------------

        def _read_body(self) -> dict | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except ValueError:
                return None

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth_key(self) -> str | None:
            return self.headers.get("Authorization")

        # -- dispatch ----------------------------------------------------

        def do_GET(self):  # noqa: N802
            self._route("GET")

        def do_POST(self):  # noqa: N802
            self._route("POST")

        def _route(self, method: str) -> None:
            path = self.path
            body = self._read_body() if method == "POST" else None
            endpoint = server._endpoints.get((method, path))  # noqa: SLF001
            if endpoint is None:
                self._fail(method, path, body, 404, "unknown_endpoint",
                           f"unknown endpoint: {method} {path}")
                return
            try:
                response = endpoint(server, self, body)
            except ApiError as exc:
                self._fail(method, path, body, exc.status, exc.code, exc.message)
                return
            except ImmichError as exc:
                self._fail(method, path, body, 500, "server_error",
                           f"Immich API error: {exc.message}")
                return
            self._send(response.status, response.payload)
            server.state.log_request(method, path, response.status, body)
            log.info("%s %s -> %d (from %s)", method, path, response.status, self.address_string())

        def _fail(self, method: str, path: str, body, status: int,
                  code: str, message: str) -> None:
            self._send(status, {"code": code, "message": message})
            server.state.log_request(method, path, status, body)
            log.warning("%s %s -> %d %s: %s (from %s)",
                        method, path, status, code, message, self.address_string())

    return Handler
