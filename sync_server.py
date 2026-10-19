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

Standalone usage:
    python sync_server.py [--port 8123] [--immich-url URL] [--immich-api-key KEY]

The Immich URL defaults to $PHOTOLAB_IMMICH_URL, then http://localhost:2283;
the API key to $PHOTOLAB_IMMICH_API_KEY.
"""

import argparse
import base64
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from immich_client import ImmichClient, ImmichError
from content_hash import compute_content_hash
from metadata import Metadata
from record import EM_DASH, MISC_EVENT, Record, display_name, format_identity, parse_identity

log = logging.getLogger("photolab-sync.server")

# Session inactivity TTL (design-api.md §2): sliding, refreshed on every
# authenticated request.
KEY_TTL_SECONDS = 60

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/tiff": ".tif",
    "image/png": ".png",
}

# Membership changes are batched this many assets per Immich call.
_ALBUM_BATCH = 500


class _ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} [{code}]: {message}")
        self.status = status
        self.code = code
        self.message = message


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


def _collect(immich: ImmichClient, on_progress) -> tuple[dict[str, Record], list[dict],
                                                         dict[str, list[str]], dict[str, str]]:
    """One full collection (design-api.md §6.2): enumerate the whole timeline,
    download every original, recompute the content hash over the pixel
    bit-stream, reconstruct the record from the embedded metadata, repair the
    album membership (design-algorithm.md §7.2), and rebuild the live maps.
    No caching: everything is re-downloaded and re-hashed.
    """
    asset_ids: list[str] = []
    for time_bucket, _count in immich.list_month_buckets():
        asset_ids.extend(immich.list_asset_ids_in_bucket(time_bucket))
    asset_ids = sorted(set(asset_ids))
    total = len(asset_ids)
    log.info("collect: %d assets to process", total)
    step = max(1, total // 10)  # log roughly every 10% of the collection

    record_by_asset: dict[str, Record] = {}
    asset_ids_by_hash: dict[str, list[str]] = {}
    albums_by_asset: dict[str, list[str]] = {}
    tmpdir = Path(tempfile.mkdtemp(prefix="photolab-sync-"))
    try:
        for i, asset_id in enumerate(asset_ids):
            if i and i % step == 0:
                log.info("collect: %d/%d assets processed", i, total)
            path = tmpdir / f"asset-{i:06d}"
            immich.download_original(asset_id, path)
            try:
                data = path.read_bytes()
                md = Metadata.from_file(path)
            finally:
                path.unlink(missing_ok=True)
            dtp = md.date_time_original
            record_by_asset[asset_id] = Record(
                year=dtp.year if dtp else None,
                month=dtp.month if dtp else None,
                event=md.event,
                subevent=md.subevent,
                category=md.category,
                supplemental_category=md.supplemental_category,
                rating=md.rating,
                date_time_original=dtp,
            )
            asset_ids_by_hash.setdefault(compute_content_hash(data), []).append(asset_id)
            albums = immich.list_albums(asset_id=asset_id)
            if albums:
                albums_by_asset[asset_id] = [a["id"] for a in albums]
            on_progress((i + 1) / total if total else 1.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    identity_to_album = _fix_album_membership(immich, record_by_asset, albums_by_asset)
    in_album = set(albums_by_asset)

    extra_copies: list[dict] = []
    records: dict[str, Record] = {}
    for h, ids in asset_ids_by_hash.items():
        primary = _pick_primary(ids, in_album)
        ids.sort(key=lambda a: (a != primary, a))   # primary first, then stable
        records[h] = record_by_asset[primary]
        extra_copies.extend({"asset_id": a, "hash": h} for a in ids if a != primary)
    return records, extra_copies, asset_ids_by_hash, identity_to_album


def _pick_primary(asset_ids: list[str], in_album: set[str]) -> str:
    """The kept copy of a duplicated hash: the sole member of an album, else
    the lowest asset id (deterministic across runs)."""
    members = [a for a in asset_ids if a in in_album]
    if len(members) == 1:
        return members[0]
    return sorted(asset_ids)[0]


def _album_canonical_identity(member_ids: list[str],
                              record_by_asset: dict[str, Record]) -> tuple | None:
    """The identity an existing album is anchored to: the majority vote over
    its members' record identities (ties broken by the lexicographically
    smallest identity); None when no member has a resolvable identity."""
    counts: dict[tuple, int] = {}
    for asset_id in member_ids:
        identity = record_by_asset[asset_id].album_identity
        if identity is not None:
            counts[identity] = counts.get(identity, 0) + 1
    if not counts:
        return None
    return max(sorted(counts), key=lambda ident: counts[ident])


def _fix_album_membership(immich: ImmichClient, record_by_asset: dict[str, Record],
                          albums_by_asset: dict[str, list[str]]) -> dict[str, str]:
    """Server pre-validation (design-algorithm.md §7.2): every asset whose
    record carries a complete 4-level identity ends up in the album of that
    identity, and in no other album.

    An existing album's identity is the majority vote over its members'
    identities; when several albums claim the same identity, the one with the
    most members wins (ties: the lexicographically smallest album id), so
    broken duplicates converge on a single album. A missing album is created
    with the identity's display name. Assets with an incomplete identity are
    left as-is (a MOVE will place them, §8). Every repair is logged. Returns
    the corrected identity -> album id map.
    """
    members_by_album: dict[str, list[str]] = {}
    for asset_id, album_ids in albums_by_asset.items():
        for album_id in album_ids:
            members_by_album.setdefault(album_id, []).append(asset_id)
    album_names = {a["id"]: a.get("albumName") or "?" for a in immich.list_albums()}

    identity_to_album: dict[str, str] = {}
    for album_id in sorted(members_by_album):
        identity = _album_canonical_identity(members_by_album[album_id], record_by_asset)
        if identity is None:
            continue
        key = format_identity(identity)
        current = identity_to_album.get(key)
        if current is None \
                or len(members_by_album[album_id]) > len(members_by_album[current]) \
                or (len(members_by_album[album_id]) == len(members_by_album[current])
                    and album_id < current):
            identity_to_album[key] = album_id

    adds: dict[str, list[str]] = {}
    removals: dict[str, list[str]] = {}
    for asset_id in sorted(record_by_asset):
        identity = record_by_asset[asset_id].album_identity
        if identity is None:
            if albums_by_asset.get(asset_id):
                log.info("album fix: asset …%s has an incomplete record; "
                         "membership left as-is", asset_id[-16:])
            continue
        key = format_identity(identity)
        target = identity_to_album.get(key)
        if target is None:
            name = display_name(identity[2], identity[3])
            target = immich.create_album(name)["id"]
            identity_to_album[key] = target
            album_names[target] = name
            log.info("album fix: created album %s (%s, id %s)", name, key, target)
        current = set(albums_by_asset.get(asset_id, []))
        if target not in current:
            adds.setdefault(target, []).append(asset_id)
            log.info("album fix: asset …%s -> %s (id %s)",
                     asset_id[-16:], album_names.get(target, "?"), target)
        for old in sorted(current - {target}):
            removals.setdefault(old, []).append(asset_id)
            log.info("album fix: asset …%s <- %s (id %s)",
                     asset_id[-16:], album_names.get(old, "?"), old)

    for album_id, asset_ids in sorted(adds.items()):
        for i in range(0, len(asset_ids), _ALBUM_BATCH):
            immich.add_assets_to_album(album_id, asset_ids[i:i + _ALBUM_BATCH])
    for album_id, asset_ids in sorted(removals.items()):
        for i in range(0, len(asset_ids), _ALBUM_BATCH):
            immich.remove_assets_from_album(album_id, asset_ids[i:i + _ALBUM_BATCH])
    if adds or removals:
        log.info("album fix: %d asset(s) added, %d asset(s) removed from wrong albums",
                 sum(len(v) for v in adds.values()), sum(len(v) for v in removals.values()))
    return identity_to_album


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
        self._endpoints = {
            ("POST", "/v1/sync/start"): self._ep_start_sync,
            ("POST", "/v1/collect"): self._ep_collect,
            ("GET", "/v1/collect/status"): self._ep_collect_status,
            ("POST", "/v1/upload"): self._ep_upload,
            ("POST", "/v1/delete"): self._ep_delete,
            ("POST", "/v1/move"): self._ep_move,
            ("POST", "/v1/album/create"): self._ep_create_album,
            ("POST", "/v1/album/delete"): self._ep_delete_album,
            ("POST", "/v1/album/rename"): self._ep_rename_album,
            ("POST", "/v1/sync/stop"): self._ep_stop_sync,
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "PhotolabSyncServer":
        server = self

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
                endpoint = server._endpoints.get((method, path))
                if endpoint is None:
                    self._fail(method, path, body, 404, "unknown_endpoint",
                               f"unknown endpoint: {method} {path}")
                    return
                try:
                    status, payload = endpoint(self, body)
                except _ApiError as exc:
                    self._fail(method, path, body, exc.status, exc.code, exc.message)
                    return
                except ImmichError as exc:
                    self._fail(method, path, body, 500, "server_error",
                               f"Immich API error: {exc.message}")
                    return
                self._send(status, payload)
                server.state.log_request(method, path, status, body)
                log.info("%s %s -> %d (from %s)", method, path, status, self.address_string())

            def _fail(self, method: str, path: str, body, status: int,
                      code: str, message: str) -> None:
                self._send(status, {"code": code, "message": message})
                server.state.log_request(method, path, status, body)
                log.warning("%s %s -> %d %s: %s (from %s)",
                            method, path, status, code, message, self.address_string())

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
            raise _ApiError(401, "invalid_key", "missing or invalid session key")

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
            raise _ApiError(410, "already_deleted",
                            f"asset {asset_id} no longer exists on the server") from None

    def _forget_asset(self, asset_id: str) -> None:
        with self.state.lock:
            for ids in self.state.asset_ids_by_hash.values():
                if asset_id in ids:
                    ids.remove(asset_id)

    # -- endpoint handlers ----------------------------------------------------

    def _ep_start_sync(self, handler, body) -> tuple[int, dict]:
        """POST /v1/sync/start — atomically idle -> active (§6.1)."""
        with self.state.lock:
            if self.state.active_key is not None:
                if time.monotonic() >= self.state.key_expires_at:
                    self.state.active_key = None  # expired -> slot is idle again (§2)
                else:
                    raise _ApiError(409, "session_conflict", "a session is already active")
            key = uuid.uuid4().hex
            self.state.active_key = key
            self.state.key_expires_at = time.monotonic() + KEY_TTL_SECONDS
            self.state.collect_job = None
        log.info("sync session started (key=%s)", key)
        return 200, {"key": key, "key_ttl_seconds": KEY_TTL_SECONDS}

    def _ep_collect(self, handler, body) -> tuple[int, dict]:
        """POST /v1/collect — start the collection job (§6.2)."""
        self._require_key(handler)
        with self.state.lock:
            if self.state.collect_job is not None:
                raise _ApiError(409, "collection_in_progress",
                                "a collection is already running")
            job = uuid.uuid4().hex
            self.state.collect_job = job
            self.state.collect_done = False
            self.state.collect_progress = 0.0
            self.state.collect_error = None
        log.info("collect started (job=%s)", job)
        server = self

        def _worker() -> None:
            try:
                records, extra, by_hash, identity = _collect(
                    server._immich, server._set_progress)
                with server.state.lock:
                    server.state.remote_records = records
                    server.state.extra_copies = extra
                    server.state.asset_ids_by_hash = by_hash
                    server.state.identity_to_album = identity
                log.info("collect done: %d photos, %d extra copies, %d albums",
                         len(records), len(extra), len(identity))
            except Exception as exc:  # noqa: BLE001 — reported to the client
                log.exception("collection failed")
                with server.state.lock:
                    server.state.collect_error = f"{type(exc).__name__}: {exc}"
            finally:
                with server.state.lock:
                    server.state.collect_done = True
                    server.state.collect_progress = 1.0

        threading.Thread(target=_worker, daemon=True).start()
        return 202, {"job": job, "status": "running", "progress": 0.0}

    def _ep_collect_status(self, handler, body) -> tuple[int, dict]:
        """GET /v1/collect/status — poll the in-flight collection (§6.2)."""
        self._require_key(handler)
        with self.state.lock:
            if self.state.collect_job is None and not self.state.collect_done:
                raise _ApiError(404, "no_collection", "no collection has been started")
            if not self.state.collect_done:
                # progress is a 0.0..1.0 fraction, monotonic, <1.0 until done.
                return 202, {"status": "running", "progress": self.state.collect_progress}
            # finished: serve the terminal state once, then clear the slot
            self.state.collect_job = None
            self.state.collect_done = False
            if self.state.collect_error is not None:
                return 500, {"code": "collect_failed", "message": self.state.collect_error}
            result = {h: r.to_dict() for h, r in self.state.remote_records.items()}
            extra = list(self.state.extra_copies)
        return 200, {"status": "done", "result": result, "extra_copies": extra}

    def _ep_upload(self, handler, body) -> tuple[int, dict]:
        """POST /v1/upload — store the client's final file as-is (§6.3)."""
        self._require_key(handler)
        if not body or not {"hash", "record", "file"} <= set(body):
            raise _ApiError(400, "bad_request",
                            "upload body must carry a hash, a record and a file")
        record = Record.from_dict(body["record"])
        album_id = self._resolve_album(body.get("album"), record)
        data = base64.b64decode(body["file"])
        log.info("Executing UPLOAD: hash …%s to album %s (size %dB)",
                 body["hash"][-16:], body.get("album"), len(data))
        ext = _EXT_BY_CONTENT_TYPE.get(body.get("content_type") or "", ".bin")
        fd, name = tempfile.mkstemp(prefix="photolab-sync-upload-", suffix=ext)
        tmp_path = Path(name)
        try:
            with open(fd, "wb") as out:
                out.write(data)
            asset = self._immich.upload(tmp_path,
                                        file_created_at=record.date_time_original)
        except ImmichError:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.unlink(missing_ok=True)
        if asset.get("status") == "duplicate":
            # v3 dedupes uploads by file bytes: a byte-identical copy exists.
            # If its asset is in the trash, restoring it is exactly what the
            # upload wanted — the same file, back in the timeline.
            try:
                existing = self._immich.get_asset(asset["id"])
            except ImmichError:
                existing = {}
            if existing.get("isTrashed"):
                self._immich.restore_trashed_asset(asset["id"])
                log.info("upload %s: restored trashed byte-identical asset %s",
                         body["hash"][:16], asset["id"])
            else:
                log.info("upload %s: byte-identical asset %s already alive",
                         body["hash"][:16], asset["id"])
        # Immich v3 ignores the albumId upload param, so membership is set
        # explicitly here for both the created and the duplicate path.
        if album_id is not None:
            self._immich.add_assets_to_album(album_id, [asset["id"]])
        with self.state.lock:
            self.state.asset_ids_by_hash.setdefault(body["hash"], []).append(asset["id"])
        log.info("upload: hash …%s -> asset %s", body["hash"][-16:], asset["id"])
        return 200, {"ok": True, "asset_id": asset["id"]}

    def _ep_delete(self, handler, body) -> tuple[int, dict]:
        """POST /v1/delete — delete by content hash or by asset id (§6.3)."""
        self._require_key(handler)
        body = body or {}
        asset_id = body.get("asset_id")
        h = body.get("hash")
        if not asset_id and not h:
            raise _ApiError(400, "bad_request", "delete body must carry a hash or asset_id")
        log.info("Executing DELETE: %s", asset_id or f"hash …{h[-16:]}")
        if asset_id:
            # Targets one of several duplicate copies (§5.2), by id.
            # A trashed id is "already gone": the bulk delete would no-op
            # with 204, but the design (§6.3) wants 410 either way.
            try:
                existing = self._immich.get_asset(asset_id)
            except ImmichError:
                existing = None
            if existing is None or existing.get("isTrashed"):
                raise _ApiError(410, "already_deleted",
                                f"asset {asset_id} no longer exists on the server")
            self._delete_asset(asset_id)
            self._forget_asset(asset_id)
            return 200, {"ok": True, "deleted": asset_id}
        with self.state.lock:
            ids = list(self.state.asset_ids_by_hash.pop(h, []))
        if not ids:
            raise _ApiError(410, "already_deleted",
                            f"no asset with content hash {h[:16]}… on the server")
        for aid in ids:
            self._delete_asset(aid)
        return 200, {"ok": True, "deleted": h}

    def _ep_move(self, handler, body) -> tuple[int, dict]:
        """POST /v1/move — rewrite the metadata, re-home into the target
        album, delete the initial file (§6.3). The pixels are untouched, so
        the content hash stays invariant."""
        self._require_key(handler)
        if not body or "hash" not in body or "record" not in body:
            raise _ApiError(400, "bad_request", "move body must carry a hash and a record")
        h = body["hash"]
        record = Record.from_dict(body["record"])
        log.info("Executing MOVE: hash …%s to album %s", h[-16:], body.get("album"))
        with self.state.lock:
            ids = self.state.asset_ids_by_hash.get(h)
            asset_id = ids[0] if ids else None
        if asset_id is None:
            raise _ApiError(410, "already_deleted",
                            f"no asset with content hash {h[:16]}… on the server")
        album_id = self._resolve_album(body.get("album"), record)
        fd, name = tempfile.mkstemp(prefix="photolab-sync-move-")
        tmp_path = Path(name)
        try:
            try:
                self._immich.download_original(asset_id, tmp_path)
            except ImmichError as exc:
                if exc.status == 404:
                    raise _ApiError(410, "already_deleted",
                                    f"asset {asset_id} no longer exists on the server") from None
                raise
            # Keep the original file name (it is display-only; the content hash
            # is over the pixel bit-stream, not the name).
            try:
                original_name = (self._immich.get_asset(asset_id).get("originalFileName")
                                 or tmp_path.name)
            except ImmichError:  # vanished mid-move: fall back to the temp name
                original_name = tmp_path.name
            named = tmp_path.with_name(Path(original_name).name)
            tmp_path.replace(named)
            tmp_path = named
            md = Metadata(
                date_time_original=record.date_time_original,
                event=record.event,
                subevent=record.subevent,
                category=record.category,
                supplemental_category=record.supplemental_category,
                rating=record.rating,
            )
            md.write_to_file(tmp_path, write_date_time=record.date_time_original is not None)
            new_asset = self._immich.upload(tmp_path,
                                            file_created_at=record.date_time_original)
        finally:
            tmp_path.unlink(missing_ok=True)
        new_id = new_asset["id"]
        if new_asset.get("status") == "duplicate":
            # A byte-identical copy of the rewritten file already exists
            # (e.g. an earlier move to the same record). Adopt it: if it is
            # in the trash, restoring it puts exactly this file back in the
            # timeline.
            try:
                existing = self._immich.get_asset(new_id)
            except ImmichError:
                existing = {}
            if existing.get("isTrashed"):
                self._immich.restore_trashed_asset(new_id)
        # Immich v3 ignores the albumId upload param, so membership is set
        # explicitly here for both the created and the duplicate path.
        if album_id is not None:
            self._immich.add_assets_to_album(album_id, [new_id])
        if new_asset.get("status") == "duplicate":
            if new_id == asset_id:
                # No-op move: the rewritten file is byte-identical to the
                # current one, so there is nothing to delete.
                with self.state.lock:
                    self.state.asset_ids_by_hash[h] = [new_id]
                return 200, {"ok": True, "moved": h, "asset_id": new_id}
        self._delete_asset(asset_id)
        with self.state.lock:
            self.state.asset_ids_by_hash[h] = [new_id]
        log.info("move: hash …%s -> asset %s (deleted old %s)", h[-16:], new_id, asset_id)
        return 200, {"ok": True, "moved": h, "asset_id": new_id}

    def _ep_create_album(self, handler, body) -> tuple[int, dict]:
        """POST /v1/album/create (§6.3)."""
        self._require_key(handler)
        if not body or "identity" not in body:
            raise _ApiError(400, "bad_request", "album/create body must carry an identity")
        identity = body["identity"]
        name = body.get("name") or ""
        log.info("Executing CREATE_ALBUM: %s (%s)", identity, name)
        with self.state.lock:
            existing = self.state.identity_to_album.get(identity)
        if existing is not None:
            return 200, {"ok": True, "identity": identity, "name": name,
                         "album_id": existing}
        # Idempotent across a crashed run: an interrupted run may have left an
        # (still empty) album with this display name — adopt it instead of
        # creating a duplicate.
        for album in self._immich.list_albums():
            if album.get("albumName") == name:
                with self.state.lock:
                    self.state.identity_to_album[identity] = album["id"]
                return 200, {"ok": True, "identity": identity, "name": name,
                             "album_id": album["id"]}
        album = self._immich.create_album(name)
        with self.state.lock:
            self.state.identity_to_album[identity] = album["id"]
        return 200, {"ok": True, "identity": identity, "name": name,
                     "album_id": album["id"]}

    def _ep_delete_album(self, handler, body) -> tuple[int, dict]:
        """POST /v1/album/delete (§6.3)."""
        self._require_key(handler)
        if not body or "identity" not in body:
            raise _ApiError(400, "bad_request", "album/delete body must carry an identity")
        identity = body["identity"]
        log.info("Executing DELETE_ALBUM: %s", identity)
        with self.state.lock:
            album_id = self.state.identity_to_album.pop(identity, None)
        if album_id is not None:
            try:
                self._immich.delete_album(album_id)
            except ImmichError as exc:
                if exc.status not in (400, 404):  # v3: unknown album -> 400
                    raise
                log.warning("album %s already gone on the server", album_id)
        return 200, {"ok": True, "identity": identity}

    def _ep_rename_album(self, handler, body) -> tuple[int, dict]:
        """POST /v1/album/rename (§6.3). A rename changes only the
        event/subevent part: the source identity's year/month are kept and the
        identity mapping is updated to the new display name."""
        self._require_key(handler)
        if not body or "identity" not in body or "new_name" not in body:
            raise _ApiError(400, "bad_request",
                            "album/rename body must carry an identity and a new_name")
        identity = body["identity"]
        new_name = body["new_name"]
        source = parse_identity(identity)
        event, sep, subevent = new_name.partition(EM_DASH)
        if not sep:
            # No dash: the target is the Misc bucket — display_name collapses
            # "Misc — X" to "X", so the whole name is the subevent.
            event, subevent = MISC_EVENT, new_name
        target_identity = format_identity((source[0], source[1], event, subevent))
        log.info("Executing RENAME_ALBUM: %s to %s",
                 display_name(source[2], source[3]), new_name)

        with self.state.lock:
            album_id = self.state.identity_to_album.get(identity)
        if album_id is None:
            # Crashed-run fallback: the album may still exist under its old
            # display name (not mappable by collect, if it has no assets).
            old_name = display_name(source[2], source[3])
            for album in self._immich.list_albums():
                if album.get("albumName") == old_name:
                    album_id = album["id"]
                    break
        if album_id is None:
            return 200, {"ok": True, "identity": identity, "new_name": new_name}
        self._immich.update_album(album_id, name=new_name)
        with self.state.lock:
            self.state.identity_to_album.pop(identity, None)
            self.state.identity_to_album[target_identity] = album_id
        return 200, {"ok": True, "identity": identity, "new_name": new_name}

    def _ep_stop_sync(self, handler, body) -> tuple[int, dict]:
        """POST /v1/sync/stop — invalidate the key, erase the empty albums,
        slot back to idle (§6.4)."""
        self._require_key(handler)
        with self.state.lock:
            self.state.active_key = None
            self.state.collect_job = None
        self._erase_empty_albums()
        log.info("sync session stopped")
        return 200, {"ok": True}

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
