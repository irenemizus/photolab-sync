"""PhotolabSyncServer — the real SyncServer, backed by the public Immich API.

Implements the REST API of docs/design-api.md and translates every operation
into calls against a live Immich instance (via immich_client). It is a "dumb
translator": all decisions are made by the SyncClient; the server applies the
client's ordered operations against Immich.

The identity (year/month/event/subevent) -> Immich album id mapping and the
content hash -> asset id mapping are rebuilt from the live Immich state on
every collect and held in memory only (design-api.md §6.2); they are then
updated in place as operations are applied within the session.

One exception to "dumb translator": the server also repairs the album
structure itself. During collect, after every record is read, the server runs
its pre-validation (design-algorithm.md §7.2): each asset with a complete
identity is placed in the album of that identity and in no other (missing
albums are created; every repair is logged). When a session ends, all empty
albums are erased.

An UPLOAD stores the client's final file bytes as-is (the client has already
augmented the metadata, design-metadata.md — no server-side tag rewrite) into
the target album. A MOVE fetches the asset's original, rewrites its embedded
metadata to the target record, uploads it directly into the target album, and
deletes the initial file (the pixels are untouched, so the content hash stays
invariant). A DELETE/MOVE target that no longer exists is reported as
``already_deleted`` (410) — non-fatal, the client re-runs the sync (§3).

The server is split across a few modules: the collection phase lives in
server_collect.py, the endpoint handlers in server_endpoints.py, and the
stdlib HTTP plumbing in server_http.py. This file ties them together: it
holds the session state (``_ServerState``), the shared endpoint helpers, and
the CLI entry point.

Standalone usage:
    python sync_server.py [--port 8123] [--immich-url URL] [--immich-api-key KEY]

The Immich URL defaults to $PHOTOLAB_IMMICH_URL, then http://localhost:2283;
the API key to $PHOTOLAB_IMMICH_API_KEY.
"""

import argparse
import logging
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from immich_client import ImmichClient, ImmichError
from record import Record
from server_endpoints import ENDPOINTS
from server_http import make_handler
from sync_api import ApiError, KEY_TTL_SECONDS

log = logging.getLogger("photolab-sync.server")


class _ServerState:
    """The mutable session/collect state, guarded by a lock."""

    def __init__(self):
        self.lock = threading.Lock()
        # the single active session slot (design-api.md §6.1)
        self.active_key: str | None = None
        self.key_expires_at: float = 0.0
        # the single in-flight collection (§6.2)
        self.collect_job: str | None = None
        self.collect_done = False
        self.collect_progress: float = 0.0
        self.collect_error: str | None = None
        # the collected remote set and its duplicate copies
        self.remote_records: dict[str, Record] = {}
        self.extra_copies: list[dict] = []
        # live maps: rebuilt on every collect, updated as ops are applied
        self.asset_ids_by_hash: dict[str, list[str]] = {}   # hash -> [primary, extras...]
        self.identity_to_album: dict[str, str] = {}         # "y/m/e/s" -> album id
        # endpoint call log (method, path, status, body)
        self.requests_log: list[dict] = []

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


class PhotolabSyncServer:
    """The real SyncServer: the REST API of design-api.md over a live Immich."""

    __test__ = False  # not a pytest test class

    def __init__(self, immich: ImmichClient, host: str = "127.0.0.1", port: int = 0):
        self._immich = immich
        self._host = host
        self._requested_port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.state = _ServerState()
        self._endpoints = ENDPOINTS

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "PhotolabSyncServer":
        self._httpd = ThreadingHTTPServer((self._host, self._requested_port),
                                          make_handler(self))
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

    def __enter__(self) -> "PhotolabSyncServer":
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

    # -- shared endpoint helpers ---------------------------------------------

    def _require_key(self, handler) -> None:
        if not self.state.check_key(handler._auth_key()):  # noqa: SLF001
            raise ApiError(401, "invalid_key", "missing or invalid session key")

    def _set_progress(self, progress: float) -> None:
        with self.state.lock:
            self.state.collect_progress = progress

    def _resolve_album(self, identity_str: str | None, record: Record | None = None) -> str | None:
        """The Immich album id for a 4-level identity, creating it on demand
        (design-api.md §6.3 upload: "ensure the album exists").

        The in-memory identity map only exists after a collect, so a fresh
        session resolves by display name first: the album name 'event —
        subevent' is the album's persistent identity across sessions (and
        crashed runs), and several identities (different year/month) may
        share one album.
        """
        if not identity_str:
            return None
        with self.state.lock:
            album_id = self.state.identity_to_album.get(identity_str)
        if album_id is not None:
            return album_id
        name = record.album_name if record is not None and record.album_name else identity_str
        for album in self._immich.list_albums():
            if album.get("albumName") == name:
                with self.state.lock:
                    self.state.identity_to_album[identity_str] = album["id"]
                return album["id"]
        album = self._immich.create_album(name)
        with self.state.lock:
            self.state.identity_to_album[identity_str] = album["id"]
        return album["id"]

    def _delete_asset(self, asset_id: str) -> None:
        """Delete from Immich; a vanished asset is `already_deleted` (§3).
        v3 reports an unknown id with 400, a live one with 204/404."""
        try:
            self._immich.delete_asset(asset_id)
        except ImmichError as exc:
            if exc.status not in (400, 404):
                raise
            raise ApiError(410, "already_deleted",
                            f"asset {asset_id} no longer exists on the server") from None

    def _forget_asset(self, asset_id: str) -> None:
        with self.state.lock:
            for ids in self.state.asset_ids_by_hash.values():
                if asset_id in ids:
                    ids.remove(asset_id)

    def _erase_empty_albums(self) -> None:
        """Erase every empty album when the session ends (design-algorithm.md §7.2)."""
        for album in self._immich.list_albums():
            if album.get("assetCount") == 0:
                self._immich.delete_album(album["id"])
                log.info("album cleanup: deleted empty album %s (id %s)",
                         album.get("albumName"), album["id"])


def _resolve_immich_credentials(args) -> tuple[str, str]:
    """CLI > env > default, per the configuration contract."""
    url = args.immich_url or os.environ.get("PHOTOLAB_IMMICH_URL") or "http://localhost:2283"
    api_key = args.immich_api_key or os.environ.get("PHOTOLAB_IMMICH_API_KEY")
    if not api_key:
        raise SystemExit("no Immich API key: pass --immich-api-key "
                         "or set PHOTOLAB_IMMICH_API_KEY")
    return url, api_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Photolab SyncServer: the real SyncServer, backed by the public Immich API.")
    parser.add_argument("--port", type=int, default=8123, help="port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind")
    parser.add_argument("--immich-url", default=None,
                        help="Immich base URL (default: $PHOTOLAB_IMMICH_URL, "
                             "then http://localhost:2283)")
    parser.add_argument("--immich-api-key", default=None,
                        help="Immich API key (default: $PHOTOLAB_IMMICH_API_KEY)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    immich_url, api_key = _resolve_immich_credentials(args)
    immich = ImmichClient(immich_url, api_key)
    major, minor, patch = immich.get_version()
    log.info("connected to Immich %d.%d.%d at %s", major, minor, patch, immich_url)
    server = PhotolabSyncServer(immich, host=args.host, port=args.port).start()
    log.info("SyncServer listening on %s", server.url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
