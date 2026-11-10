"""The SyncServer's endpoint handlers (docs/design-api.md §6).

Each handler translates one REST call into operations against the live
Immich instance. A handler is a plain function taking the server (for its
state, shared helpers and ImmichClient) and the request handler (for the
session key); the (method, path) -> handler dispatch table is ENDPOINTS,
wired into PhotolabSyncServer by sync_server.py.
"""

import base64
import logging
import tempfile
import threading
import time
import uuid
from pathlib import Path

from immich_client import ImmichError
from metadata import Metadata
from record import (EM_DASH, MISC_EVENT, AlbumIdentity, Record, display_name,
                    format_identity, parse_identity)
from server_collect import _collect
from sync_api import ApiError, KEY_TTL_SECONDS, Response

log = logging.getLogger("photolab-sync.server")

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/tiff": ".tif",
    "image/png": ".png",
}


def _ep_start_sync(server, handler, body) -> Response:
    """POST /v1/sync/start — atomically idle -> active (§6.1)."""
    with server.state.lock:
        if server.state.active_key is not None:
            if time.monotonic() >= server.state.key_expires_at:
                server.state.active_key = None  # expired -> slot is idle again (§2)
            else:
                raise ApiError(409, "session_conflict", "a session is already active")
        key = uuid.uuid4().hex
        server.state.active_key = key
        server.state.key_expires_at = time.monotonic() + KEY_TTL_SECONDS
        server.state.collect_job = None
    log.info("sync session started (key=%s)", key)
    return Response(200, {"key": key, "key_ttl_seconds": KEY_TTL_SECONDS})


def _ep_collect(server, handler, body) -> Response:
    """POST /v1/collect — start the collection job (§6.2)."""
    server._require_key(handler)  # noqa: SLF001
    with server.state.lock:
        if server.state.collect_job is not None:
            raise ApiError(409, "collection_in_progress",
                           "a collection is already running")
        job = uuid.uuid4().hex
        server.state.collect_job = job
        server.state.collect_done = False
        server.state.collect_progress = 0.0
        server.state.collect_error = None
    log.info("collect started (job=%s)", job)

    def _worker() -> None:
        try:
            records, extra, by_hash, identity = _collect(server._immich,  # noqa: SLF001
                                                         server._set_progress)
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
    return Response(202, {"job": job, "status": "running", "progress": 0.0})


def _ep_collect_status(server, handler, body) -> Response:
    """GET /v1/collect/status — poll the in-flight collection (§6.2)."""
    server._require_key(handler)  # noqa: SLF001
    with server.state.lock:
        if server.state.collect_job is None and not server.state.collect_done:
            raise ApiError(404, "no_collection", "no collection has been started")
        if not server.state.collect_done:
            # progress is a 0.0..1.0 fraction, monotonic, <1.0 until done.
            return Response(202, {"status": "running", "progress": server.state.collect_progress})
        # finished: serve the terminal state once, then clear the slot
        server.state.collect_job = None
        server.state.collect_done = False
        if server.state.collect_error is not None:
            return Response(500, {"code": "collect_failed", "message": server.state.collect_error})
        result = {h: r.to_dict() for h, r in server.state.remote_records.items()}
        extra = list(server.state.extra_copies)
    return Response(200, {"status": "done", "result": result, "extra_copies": extra})


def _ep_upload(server, handler, body) -> Response:
    """POST /v1/upload — store the client's final file as-is (§6.3)."""
    server._require_key(handler)  # noqa: SLF001
    if not body or not {"hash", "record", "file"} <= set(body):
        raise ApiError(400, "bad_request",
                       "upload body must carry a hash, a record and a file")
    record = Record.from_dict(body["record"])
    album_id = server._resolve_album(body.get("album"), record)  # noqa: SLF001
    data = base64.b64decode(body["file"])
    log.info("Executing UPLOAD: hash …%s to album %s (size %dB)",
             body["hash"][-16:], body.get("album"), len(data))
    ext = _EXT_BY_CONTENT_TYPE.get(body.get("content_type") or "", ".bin")
    fd, name = tempfile.mkstemp(prefix="photolab-sync-upload-", suffix=ext)
    tmp_path = Path(name)
    try:
        with open(fd, "wb") as out:
            out.write(data)
        asset = server._immich.upload(tmp_path,  # noqa: SLF001
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
            existing = server._immich.get_asset(asset["id"])  # noqa: SLF001
        except ImmichError:
            existing = {}
        if existing.get("isTrashed"):
            server._immich.restore_trashed_asset(asset["id"])  # noqa: SLF001
            log.info("upload %s: restored trashed byte-identical asset %s",
                     body["hash"][:16], asset["id"])
        else:
            log.info("upload %s: byte-identical asset %s already alive",
                     body["hash"][:16], asset["id"])
    # Immich v3 ignores the albumId upload param, so membership is set
    # explicitly here for both the created and the duplicate path.
    if album_id is not None:
        server._immich.add_assets_to_album(album_id, [asset["id"]])  # noqa: SLF001
    with server.state.lock:
        server.state.asset_ids_by_hash.setdefault(body["hash"], []).append(asset["id"])
    log.info("upload: hash …%s -> asset %s", body["hash"][-16:], asset["id"])
    return Response(200, {"ok": True, "asset_id": asset["id"]})


def _ep_delete(server, handler, body) -> Response:
    """POST /v1/delete — delete by content hash or by asset id (§6.3)."""
    server._require_key(handler)  # noqa: SLF001
    body = body or {}
    asset_id = body.get("asset_id")
    h = body.get("hash")
    if not asset_id and not h:
        raise ApiError(400, "bad_request", "delete body must carry a hash or asset_id")
    log.info("Executing DELETE: %s", asset_id or f"hash …{h[-16:]}")
    if asset_id:
        # Targets one of several duplicate copies (§5.2), by id.
        # A trashed id is "already gone": the bulk delete would no-op
        # with 204, but the design (§6.3) wants 410 either way.
        try:
            existing = server._immich.get_asset(asset_id)  # noqa: SLF001
        except ImmichError:
            existing = None
        if existing is None or existing.get("isTrashed"):
            raise ApiError(410, "already_deleted",
                           f"asset {asset_id} no longer exists on the server")
        server._delete_asset(asset_id)  # noqa: SLF001
        server._forget_asset(asset_id)  # noqa: SLF001
        return Response(200, {"ok": True, "deleted": asset_id})
    with server.state.lock:
        ids = list(server.state.asset_ids_by_hash.pop(h, []))
    if not ids:
        raise ApiError(410, "already_deleted",
                       f"no asset with content hash {h[:16]}… on the server")
    for aid in ids:
        server._delete_asset(aid)  # noqa: SLF001
    return Response(200, {"ok": True, "deleted": h})


def _ep_move(server, handler, body) -> Response:
    """POST /v1/move — rewrite the metadata, re-home into the target
    album, delete the initial file (§6.3). The pixels are untouched, so
    the content hash stays invariant."""
    server._require_key(handler)  # noqa: SLF001
    if not body or "hash" not in body or "record" not in body:
        raise ApiError(400, "bad_request", "move body must carry a hash and a record")
    h = body["hash"]
    record = Record.from_dict(body["record"])
    log.info("Executing MOVE: hash …%s to album %s", h[-16:], body.get("album"))
    with server.state.lock:
        ids = server.state.asset_ids_by_hash.get(h)
        asset_id = ids[0] if ids else None
    if asset_id is None:
        raise ApiError(410, "already_deleted",
                       f"no asset with content hash {h[:16]}… on the server")
    album_id = server._resolve_album(body.get("album"), record)  # noqa: SLF001
    fd, name = tempfile.mkstemp(prefix="photolab-sync-move-")
    tmp_path = Path(name)
    try:
        try:
            server._immich.download_original(asset_id, tmp_path)  # noqa: SLF001
        except ImmichError as exc:
            if exc.status == 404:
                raise ApiError(410, "already_deleted",
                               f"asset {asset_id} no longer exists on the server") from None
            raise
        # Keep the original file name (it is display-only; the content hash
        # is over the pixel bit-stream, not the name).
        try:
            original_name = (server._immich.get_asset(asset_id)  # noqa: SLF001
                             .get("originalFileName") or tmp_path.name)
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
        new_asset = server._immich.upload(tmp_path,  # noqa: SLF001
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
            existing = server._immich.get_asset(new_id)  # noqa: SLF001
        except ImmichError:
            existing = {}
        if existing.get("isTrashed"):
            server._immich.restore_trashed_asset(new_id)  # noqa: SLF001
    # Immich v3 ignores the albumId upload param, so membership is set
    # explicitly here for both the created and the duplicate path.
    if album_id is not None:
        server._immich.add_assets_to_album(album_id, [new_id])  # noqa: SLF001
    if new_asset.get("status") == "duplicate":
        if new_id == asset_id:
            # No-op move: the rewritten file is byte-identical to the
            # current one, so there is nothing to delete.
            with server.state.lock:
                server.state.asset_ids_by_hash[h] = [new_id]
            return Response(200, {"ok": True, "moved": h, "asset_id": new_id})
    server._delete_asset(asset_id)  # noqa: SLF001
    with server.state.lock:
        server.state.asset_ids_by_hash[h] = [new_id]
    log.info("move: hash …%s -> asset %s (deleted old %s)", h[-16:], new_id, asset_id)
    return Response(200, {"ok": True, "moved": h, "asset_id": new_id})


def _ep_create_album(server, handler, body) -> Response:
    """POST /v1/album/create (§6.3)."""
    server._require_key(handler)  # noqa: SLF001
    if not body or "identity" not in body:
        raise ApiError(400, "bad_request", "album/create body must carry an identity")
    identity = body["identity"]
    name = body.get("name") or ""
    log.info("Executing CREATE_ALBUM: %s (%s)", identity, name)
    with server.state.lock:
        existing = server.state.identity_to_album.get(identity)
    if existing is not None:
        return Response(200, {"ok": True, "identity": identity, "name": name,
                              "album_id": existing})
    # Idempotent across a crashed run: an interrupted run may have left an
    # (still empty) album with this display name — adopt it instead of
    # creating a duplicate.
    for album in server._immich.list_albums():  # noqa: SLF001
        if album.get("albumName") == name:
            with server.state.lock:
                server.state.identity_to_album[identity] = album["id"]
            return Response(200, {"ok": True, "identity": identity, "name": name,
                                  "album_id": album["id"]})
    album = server._immich.create_album(name)  # noqa: SLF001
    with server.state.lock:
        server.state.identity_to_album[identity] = album["id"]
    return Response(200, {"ok": True, "identity": identity, "name": name,
                          "album_id": album["id"]})


def _ep_delete_album(server, handler, body) -> Response:
    """POST /v1/album/delete (§6.3)."""
    server._require_key(handler)  # noqa: SLF001
    if not body or "identity" not in body:
        raise ApiError(400, "bad_request", "album/delete body must carry an identity")
    identity = body["identity"]
    log.info("Executing DELETE_ALBUM: %s", identity)
    with server.state.lock:
        album_id = server.state.identity_to_album.pop(identity, None)
    if album_id is not None:
        try:
            server._immich.delete_album(album_id)  # noqa: SLF001
        except ImmichError as exc:
            if exc.status not in (400, 404):  # v3: unknown album -> 400
                raise
            log.warning("album %s already gone on the server", album_id)
    return Response(200, {"ok": True, "identity": identity})


def _ep_rename_album(server, handler, body) -> Response:
    """POST /v1/album/rename (§6.3). A rename changes only the
    event/subevent part: the source identity's year/month are kept and the
    identity mapping is updated to the new display name."""
    server._require_key(handler)  # noqa: SLF001
    if not body or "identity" not in body or "new_name" not in body:
        raise ApiError(400, "bad_request",
                       "album/rename body must carry an identity and a new_name")
    identity = body["identity"]
    new_name = body["new_name"]
    source = parse_identity(identity)
    event, sep, subevent = new_name.partition(EM_DASH)
    if not sep:
        # No dash: the target is the Misc bucket — display_name collapses
        # "Misc — X" to "X", so the whole name is the subevent.
        event, subevent = MISC_EVENT, new_name
    target_identity = format_identity(AlbumIdentity(source.year, source.month, event, subevent))
    log.info("Executing RENAME_ALBUM: %s to %s",
             display_name(source.event, source.subevent), new_name)

    with server.state.lock:
        album_id = server.state.identity_to_album.get(identity)
    if album_id is None:
        # Crashed-run fallback: the album may still exist under its old
        # display name (not mappable by collect, if it has no assets).
        old_name = display_name(source.event, source.subevent)
        for album in server._immich.list_albums():  # noqa: SLF001
            if album.get("albumName") == old_name:
                album_id = album["id"]
                break
    if album_id is None:
        return Response(200, {"ok": True, "identity": identity, "new_name": new_name})
    server._immich.update_album(album_id, name=new_name)  # noqa: SLF001
    with server.state.lock:
        server.state.identity_to_album.pop(identity, None)
        server.state.identity_to_album[target_identity] = album_id
    return Response(200, {"ok": True, "identity": identity, "new_name": new_name})


def _ep_stop_sync(server, handler, body) -> Response:
    """POST /v1/sync/stop — invalidate the key, erase the empty albums,
    slot back to idle (§6.4)."""
    server._require_key(handler)  # noqa: SLF001
    with server.state.lock:
        server.state.active_key = None
        server.state.collect_job = None
    server._erase_empty_albums()  # noqa: SLF001
    log.info("sync session stopped")
    return Response(200, {"ok": True})


ENDPOINTS = {
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
