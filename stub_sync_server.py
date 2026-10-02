"""TestStubSyncServer — a stub SyncServer for developing the SyncClient.

Implements the REST API of docs/design-api.md with stdlib http.server: every
endpoint call is logged (`requests_log`) and each response is a friendly,
spec-shaped JSON. The collect endpoint reports a completely empty remote set
(no photos, no extra copies), so a full client run against the stub produces a
pure upload plan.

Session semantics follow the spec: one atomic active session slot
(§6.1), sliding inactivity TTL (§2), one collection at a time (§6.2), and the
standard error codes (§4). It is NOT a production server — there is no Immich
behind it.

Standalone usage:
    python stub_sync_server.py [--port 8123]
"""

import argparse
import base64
import json
import logging
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("photolab-sync.stub-server")

# Session inactivity TTL (design-api.md §2): sliding, refreshed on every
# authenticated request.
KEY_TTL_SECONDS = 60


class _ServerState:
    """The mutable session/collection state, guarded by a lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active_key: str | None = None
        self.key_expires_at: float = 0.0
        self.collect_job: str | None = None     # id of the running collection
        self.collect_done_polls = 0             # running polls served so far
        self.running_polls_before_done = 1      # how many "running" polls to serve
        self.requests_log: list[dict] = []
        self.uploads: dict[str, dict] = {}      # hash -> last upload payload (sans file)

    def check_key(self, key: str | None) -> bool:
        """Under lock: is `key` the active, unexpired session key?"""
        with self.lock:
            if key is None or key != self.active_key:
                return False
            if time.monotonic() >= self.key_expires_at:
                self.active_key = None          # expired -> slot returns to idle (§2)
                return False
            self.key_expires_at = time.monotonic() + KEY_TTL_SECONDS  # sliding (§2)
            return True

    def log_request(self, method: str, path: str, status: int, body: dict | None) -> None:
        with self.lock:
            self.requests_log.append(
                {"method": method, "path": path, "status": status, "body": body})


class TestStubSyncServer:
    """A stub SyncServer for tests and manual SyncClient runs.

    - ephemeral port by default (`start()`); pass a fixed port to pin it
    - `requests_log`: one entry per endpoint call (method, path, status, body)
    - `uploads`: {hash: {album, record, content_type, file_size}} of /v1/upload
    - collect reports an empty remote set (result {}, extra_copies [])
    """

    __test__ = False  # not a pytest test class, despite the Test* name

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._requested_port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.state = _ServerState()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "TestStubSyncServer":
        state = self.state

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
                # Every response is a 2xx with a JSON body (no 1xx in the spec).
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
                handler = _ENDPOINTS.get((method, path))
                if handler is None:
                    self._handle_error(method, path, body, 404, "unknown_endpoint",
                                       f"unknown endpoint: {method} {path}")
                    return
                try:
                    status, payload = handler(self, state, body)
                except _ApiError as exc:
                    self._handle_error(method, path, body, exc.status, exc.code, exc.message)
                    return
                self._send(status, payload)
                state.log_request(method, path, status, body)
                log.debug("%s %s -> %s", method, path, status)

            def _handle_error(self, method: str, path: str, body, status: int,
                              code: str, message: str) -> None:
                self._send(status, {"code": code, "message": message})
                state.log_request(method, path, status, body)
                log.debug("%s %s -> %s %s", method, path, status, code)

        self._httpd = ThreadingHTTPServer((self._host, self._requested_port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> "TestStubSyncServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- accessors ----------------------------------------------------------

    @property
    def url(self) -> str:
        assert self._httpd is not None, "server is not started"
        return f"http://{self._httpd.server_address[0]}:{self._httpd.server_address[1]}"

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server is not started"
        return self._httpd.server_address[1]

    def calls(self, path: str | None = None) -> list[dict]:
        """Logged endpoint calls, optionally filtered by exact path."""
        return [c for c in self.state.requests_log
                if path is None or c["path"] == path]

    def set_collect_running_polls(self, n: int) -> None:
        """Serve `n` "running" polls before the collect reports done (default 1)."""
        self.state.running_polls_before_done = max(0, n)


class _ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} [{code}]: {message}")
        self.status = status
        self.code = code
        self.message = message


# -- endpoint handlers ---------------------------------------------------------

def _ep_start_sync(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/sync/start — atomically idle -> active (§6.1)."""
    with state.lock:
        if state.active_key is not None:
            if time.monotonic() >= state.key_expires_at:
                state.active_key = None  # expired -> slot is idle again (§2)
            else:
                raise _ApiError(409, "session_conflict", "a session is already active")
        key = uuid.uuid4().hex
        state.active_key = key
        state.key_expires_at = time.monotonic() + KEY_TTL_SECONDS
        state.collect_job = None
    return 200, {"key": key, "key_ttl_seconds": KEY_TTL_SECONDS}


def _ep_collect(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/collect — start the (stub) collection job (§6.2)."""
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    with state.lock:
        if state.collect_job is not None:
            raise _ApiError(409, "collection_in_progress",
                            "a collection is already running")
        job = uuid.uuid4().hex
        state.collect_job = job
        state.collect_done_polls = 0
    return 202, {"job": job, "status": "running", "progress": 0.0}


def _ep_collect_status(handler, state: _ServerState, body) -> tuple[int, dict]:
    """GET /v1/collect/status — poll the (stub) collection (§6.2).

    Reports an empty remote set: no photos, no extra copies.
    """
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    with state.lock:
        if state.collect_job is None:
            raise _ApiError(404, "no_collection", "no collection has been started")
        if state.collect_done_polls < state.running_polls_before_done:
            state.collect_done_polls += 1
            # progress is a 0.0..1.0 fraction, monotonic, <1.0 until done.
            progress = min(0.99, state.collect_done_polls * 0.5)
            return 202, {"status": "running", "progress": progress}
        state.collect_job = None
        return 200, {"status": "done", "result": {}, "extra_copies": []}


def _ep_upload(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/upload — accept the final file, keep a copy of the payload (§6.3)."""
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    if not body or "hash" not in body:
        raise _ApiError(400, "bad_request", "upload body must carry a hash")
    asset_id = uuid.uuid4().hex
    with state.lock:
        state.uploads[body["hash"]] = {
            "asset_id": asset_id,
            "album": body.get("album"),
            "record": body.get("record"),
            "content_type": body.get("content_type"),
            # file_size is the decoded payload size, not the base64 string length
            "file_size": len(base64.b64decode(body.get("file", ""))),
        }
    return 200, {"ok": True, "asset_id": asset_id}


def _ep_delete(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/delete — pretend to delete (§6.3)."""
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    target = (body or {}).get("hash") or (body or {}).get("asset_id")
    if target is None:
        raise _ApiError(400, "bad_request", "delete body must carry a hash or asset_id")
    return 200, {"ok": True, "deleted": target}


def _ep_move(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/move — pretend to move + rewrite metadata (§6.3)."""
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    if not body or "hash" not in body:
        raise _ApiError(400, "bad_request", "move body must carry a hash")
    refreshed = body.get("refreshed_file")
    return 200, {"ok": True, "moved": body["hash"],
                 "refreshed": refreshed is not None}


def _ep_create_album(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/album/create (§6.3)."""
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    if not body or "identity" not in body:
        raise _ApiError(400, "bad_request", "album/create body must carry an identity")
    return 200, {"ok": True, "identity": body["identity"], "name": body.get("name")}


def _ep_delete_album(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/album/delete (§6.3)."""
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    if not body or "identity" not in body:
        raise _ApiError(400, "bad_request", "album/delete body must carry an identity")
    return 200, {"ok": True, "identity": body["identity"]}


def _ep_rename_album(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/album/rename (§6.3)."""
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    if not body or "identity" not in body:
        raise _ApiError(400, "bad_request", "album/rename body must carry an identity")
    return 200, {"ok": True, "identity": body["identity"],
                 "new_name": body.get("new_name")}


def _ep_stop_sync(handler, state: _ServerState, body) -> tuple[int, dict]:
    """POST /v1/sync/stop — invalidate the key, slot back to idle (§6.4)."""
    if not state.check_key(handler._auth_key()):  # noqa: SLF001
        raise _ApiError(401, "invalid_key", "missing or invalid session key")
    with state.lock:
        state.active_key = None
        state.collect_job = None
    return 200, {"ok": True}


_ENDPOINTS = {
    ("POST", "/v1/sync/start"): _ep_start_sync,
    ("POST", "/v1/collect"): _ep_collect,
    ("GET", "/v1/collect/status"): _ep_collect_status,
    ("POST", "/v1/upload"): _ep_upload,
    ("POST", "/v1/delete"): _ep_delete,
    ("POST", "/v1/move"): _ep_move,
    ("POST", "/v1/album/create"): _ep_create_album,
    ("POST", "/v1/album/delete"): _ep_delete_album,
    ("POST", "/v1/album/rename"): _ep_rename_album,
    ("POST", "/v1/sync/stop"): _ep_stop_sync,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Photolab TestStubSyncServer: a stub SyncServer that logs every call.")
    parser.add_argument("--port", type=int, default=8123, help="port to listen on")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = TestStubSyncServer(port=args.port).start()
    log.info("stub SyncServer listening on %s (collect reports an empty remote set)",
             server.url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
