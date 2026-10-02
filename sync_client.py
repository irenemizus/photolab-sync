"""SyncClient — the smart side of Photolab Sync (docs/design.md).

The SyncClient is the brain: it scans the local 7-level tree, prepares each
image's final file (pixels + augmented metadata, docs/design-metadata.md),
computes the content SHA512 (over the raw file bytes), validates the local set,
builds the ordered list of atomic operations (docs/design-algorithm.md), and
drives the session against the SyncServer REST API (docs/design-api.md):

    sync/start -> collect -> poll collect/status -> apply ops in order -> sync/stop

The final file (the upload payload) is kept in memory, keyed by content hash.
A crashed sync is resumed by re-running it: operations are idempotent and a
fresh collect re-converges (docs/design-algorithm.md §6.2). A mid-sync
"already_deleted" is non-fatal and triggers a full re-run (docs/design-api.md
§3); a lost server (repeated connection failures) is fatal.
"""

import argparse
import base64
import calendar
import logging
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

from algorithm_builder import (
    Operation,
    OperationType,
    build_sync_algorithm,
    validate_local_pairs,
)
from metadata import Metadata
from place import Place
from content_hash import compute_content_hash
from record import Record, format_identity

log = logging.getLogger("photolab-sync.client")

# Supported formats (docs/design.md): jpeg, tiff, png. No RAW.
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".png": "image/png",
}

# The local tree layout is exactly 7 directory levels above the file name
# (docs/design-metadata.md).
LEVELS = 7


class SyncError(Exception):
    """Fatal error: the sync is aborted and reported to the user."""


class ServerLostError(SyncError):
    """The server went away mid-session (docs/design-api.md §3, fatal)."""


class AlreadyDeletedError(SyncError):
    """Mid-sync "already deleted" (docs/design-api.md §3/§4, non-fatal).

    The client logs it and re-runs the sync from the very beginning.
    """


def _error_payload(resp: requests.Response) -> tuple[str, str]:
    try:
        data = resp.json()
        return str(data.get("code", "unknown")), str(data.get("message", resp.text))
    except ValueError:
        return "unknown", resp.text


class SyncApiClient:
    """Thin client for the SyncServer REST API (docs/design-api.md).

    Plain HTTP, no security beyond the opaque one-time session key, which is
    returned by sync/start and sent as `Authorization: <key>` on every other
    call. Long work (collect) is "start (202)" + "poll status"; no single
    request blocks for long.
    """

    def __init__(self, base_url: str, *,
                 connect_timeout: float = 5.0,
                 read_timeout: float = 600.0,
                 max_retries: int = 3,
                 retry_backoff: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._key: str | None = None

    # -- session ---------------------------------------------------------

    def start_sync(self) -> str:
        """POST /v1/sync/start — begin a session, returns the one-time key (§6.1)."""
        data = self._request("POST", "/v1/sync/start", auth=False).json()
        self._key = data["key"]
        log.info("sync session started (key ttl %ss)", data.get("key_ttl_seconds"))
        return self._key

    def stop_sync(self) -> None:
        """POST /v1/sync/stop — invalidate the key, release the session (§6.4)."""
        self._request("POST", "/v1/sync/stop", body={})
        self._key = None
        log.info("sync session stopped")

    # -- collect ---------------------------------------------------------

    def collect(self) -> None:
        """POST /v1/collect — start the collection job (§6.2).

        The 202 response carries the job id + progress; the client does not need
        them here — collect/status polls the session's single in-flight
        collection.
        """
        self._request("POST", "/v1/collect", body={})

    def collect_status(self) -> dict:
        """GET /v1/collect/status — poll the in-flight collection (§6.2).

        202 = running (body has progress), 200 = done (body has result +
        extra_copies). Both carry a JSON body, so it is returned as-is.
        """
        return self._request("GET", "/v1/collect/status").json()

    # -- atomic operations (applied in client-given order, §6.3) ----------

    def upload(self, h: str, record: Record, ext: str, file_bytes: bytes) -> None:
        body = {
            "hash": h,
            "album": format_identity(record.album_identity),
            "record": record.to_dict(),
            "content_type": CONTENT_TYPES[ext],
            "file": base64.b64encode(file_bytes).decode("ascii"),
        }
        self._request("POST", "/v1/upload", body=body)

    def delete(self, h: str | None, asset_id: str | None) -> None:
        # asset_id targets one of several duplicate copies (§6.3); otherwise
        # the photo is addressed by its content hash.
        body = {"asset_id": asset_id} if asset_id else {"hash": h}
        self._request("POST", "/v1/delete", body=body)

    def move(self, h: str, record: Record, refreshed_file: bytes | None) -> None:
        body = {
            "hash": h,
            "album": format_identity(record.album_identity),
            "record": record.to_dict(),
        }
        if refreshed_file is not None:
            body["refreshed_file"] = base64.b64encode(refreshed_file).decode("ascii")
        self._request("POST", "/v1/move", body=body)

    def create_album(self, identity: tuple, name: str) -> None:
        self._request("POST", "/v1/album/create",
                      body={"identity": format_identity(identity), "name": name})

    def delete_album(self, identity: tuple) -> None:
        self._request("POST", "/v1/album/delete", body={"identity": format_identity(identity)})

    def rename_album(self, identity: tuple, new_name: str) -> None:
        self._request("POST", "/v1/album/rename",
                      body={"identity": format_identity(identity), "new_name": new_name})

    # -- transport --------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 body: dict | None = None, auth: bool = True) -> requests.Response:
        url = self.base_url + path
        headers = {}
        if auth:
            if self._key is None:
                raise SyncError("no session key; call start_sync() first")
            headers["Authorization"] = self._key
        retries_left = self._max_retries
        while True:
            try:
                resp = self._session.request(
                    method, url, json=body, headers=headers,
                    timeout=(self._connect_timeout, self._read_timeout),
                )
                break
            except requests.ConnectionError as exc:
                # A few retries, then the server is declared lost (§3).
                retries_left -= 1
                if retries_left < 0:
                    raise ServerLostError(f"server lost: {exc}") from exc
                log.warning("connection to %s failed, %d retries left: %s",
                            url, retries_left, exc)
                time.sleep(self._retry_backoff)
        if resp.status_code >= 400:
            code, message = _error_payload(resp)
            if code == "already_deleted":
                raise AlreadyDeletedError(f"{method} {path}: {message}")
            raise SyncError(f"{method} {path} -> HTTP {resp.status_code} [{code}]: {message}")
        return resp


@dataclass
class SyncResult:
    operations: list[Operation] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    remote_photos: int = 0

    def summary(self) -> str:
        if not self.counts:
            return "no operations"
        return ", ".join(f"{self.counts[t]} {t.value}"
                         for t in OperationType if t in self.counts)


def _date_time_for(file_dt: datetime | None, place: Place) -> datetime:
    """The date-time rule (docs/design-metadata.md, step 4).

    The path carries only year/month; the day and time are preserved from the
    file's own date_time_original. Only when the file has no original
    date-time do we materialise the fallback year/month/01 00:00:00.
    """
    if file_dt is None:
        return datetime(place.year, place.month, 1)
    day = min(file_dt.day, calendar.monthrange(place.year, place.month)[1])
    return file_dt.replace(year=place.year, month=place.month, day=day)


class SyncClient:
    def __init__(self, root: Path | str, server_url: str, *,
                 poll_interval: float = 1.0,
                 collect_timeout: float = 3600.0,
                 max_sync_restarts: int = 3,
                 api: SyncApiClient | None = None):
        self.root = Path(root)
        self.api = api or SyncApiClient(server_url)
        self._poll_interval = poll_interval
        self._collect_timeout = collect_timeout
        self._max_sync_restarts = max_sync_restarts
        self._files: dict[str, bytes] = {}      # content hash -> final file bytes
        self._extensions: dict[str, str] = {}   # content hash -> file extension

    # -- entry point ------------------------------------------------------

    def run(self) -> SyncResult:
        """Run one full synchronization: scan -> validate -> session -> apply.

        A client-side validation failure (local duplicate content) aborts
        before any session is started (design-algorithm.md §7). A mid-sync
        "already_deleted" is non-fatal: the sync is re-run from the beginning
        (design-api.md §3), up to max_sync_restarts times.
        """
        local_pairs = self.scan()
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._run_session(local_pairs)
            except AlreadyDeletedError as exc:
                if attempt >= self._max_sync_restarts:
                    raise SyncError(
                        f"sync did not converge after {attempt} runs: {exc}"
                    ) from exc
                log.warning("already_deleted mid-sync (run %d): %s — "
                            "re-running the sync from the beginning", attempt, exc)

    def _run_session(self, local_pairs: dict[str, Record]) -> SyncResult:
        self.api.start_sync()
        try:
            self.api.collect()
            log.info("collection started")
            remote_pairs, extra_copies = self._wait_for_collect()
            log.info("collection done: %d remote photos (%d extra copies)",
                     len(remote_pairs), len(extra_copies))
            operations = build_sync_algorithm(local_pairs, remote_pairs, extra_copies)
            counts = dict(Counter(op.operation_type for op in operations))
            log.info("plan: %d operations", len(operations))
            for op in operations:
                self._apply(op)
                log.info("applied: %s", op)
        finally:
            try:
                self.api.stop_sync()
            except SyncError as exc:
                log.warning("sync/stop failed (session may have expired): %s", exc)
        return SyncResult(operations=operations, counts=counts,
                          remote_photos=len(remote_pairs))

    def _wait_for_collect(self) -> tuple[dict[str, Record], list[dict]]:
        deadline = time.monotonic() + self._collect_timeout
        while True:
            data = self.api.collect_status()
            status = data.get("status")
            if status == "done":
                result = data.get("result") or {}
                remote_pairs = {h: Record.from_dict(r) for h, r in result.items()}
                return remote_pairs, data.get("extra_copies") or []
            if status == "running":
                if time.monotonic() > deadline:
                    raise SyncError(
                        f"collection did not finish within {self._collect_timeout:.0f}s")
                log.info("collection in progress: %d%%",
                         round(100 * float(data.get("progress", 0.0))))
                time.sleep(self._poll_interval)
                continue
            raise SyncError(f"unexpected collection status: {status!r}")

    # -- local side --------------------------------------------------------

    def scan(self) -> dict[str, Record]:
        """Walk the tree, run the upload-preparation pipeline on every
        supported image (design-metadata.md), and validate the local set.

        Returns {content_hash: Record}; the final files are kept in memory,
        keyed by content hash, ready to be sent as upload/move payloads.
        """
        self._files.clear()
        self._extensions.clear()
        pending: list[tuple[Path, Path, str, Record, str]] = []
        temps: list[Path] = []
        try:
            for path in self._walk():
                rel = path.relative_to(self.root)
                place = self._parse_place(rel)
                if place is None:
                    continue
                md = Metadata.from_file(path)
                md.patch_from_place(place)
                md.date_time_original = _date_time_for(md.date_time_original, place)
                tmp = self._write_final_file(path, md)
                temps.append(tmp)
                h = compute_content_hash(tmp.read_bytes())
                rec = Record(
                    year=place.year,
                    month=place.month,
                    event=md.event,
                    subevent=md.subevent,
                    category=md.category,
                    supplemental_category=md.supplemental_category,
                    rating=md.rating,
                    date_time_original=md.date_time_original,
                )
                pending.append((path, tmp, h, rec, path.suffix.lower()))
            # Fatal pre-check (design-algorithm.md §7) BEFORE anything is
            # applied — even the local metadata write-backs below wait for it.
            validate_local_pairs([(h, rec) for _, _, h, rec, _ in pending])
        except BaseException:
            for tmp in temps:
                tmp.unlink(missing_ok=True)
            raise
        for path, tmp, h, rec, ext in pending:
            data = tmp.read_bytes()
            os.replace(tmp, path)
            self._files[h] = data
            self._extensions[h] = ext
        local_pairs = {h: rec for _, _, h, rec, _ in pending}
        log.info("scanned %d images from %s", len(local_pairs), self.root)
        return local_pairs

    def _walk(self):
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            rel = path.relative_to(self.root)
            if len(rel.parts) != LEVELS + 1:
                log.warning("skipping %s: expected %d directory levels, got %d",
                            rel, LEVELS, len(rel.parts) - 1)
                continue
            yield path

    def _parse_place(self, rel: Path) -> Place | None:
        try:
            return Place.from_path_string(rel)
        except (ValueError, KeyError, IndexError) as exc:
            log.warning("skipping %s: cannot parse place (%s)", rel, exc)
            return None

    @staticmethod
    def _write_final_file(path: Path, md: Metadata) -> Path:
        """Write the augmented metadata into a same-directory temp copy.

        The metadata write is lossless w.r.t. pixels (design-metadata.md
        invariants), so the temp copy's pixels equal the original's. The copy
        is the final file; it is atomically swapped over the original by
        scan() after validation passes.
        """
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(fd)
        tmp = Path(name)
        try:
            shutil.copyfile(path, tmp)
            md.write_to_file(tmp, write_date_time=True)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    # -- apply -------------------------------------------------------------

    def _apply(self, op: Operation) -> None:
        t = op.operation_type
        if t is OperationType.UPLOAD:
            self.api.upload(op.hash, op.record, self._extensions[op.hash], self._files[op.hash])
        elif t is OperationType.DELETE:
            self.api.delete(op.hash, op.asset_id)
        elif t is OperationType.MOVE:
            refreshed = None
            if op.from_record.album_identity == op.record.album_identity:
                refreshed = self._files[op.hash]
            self.api.move(op.hash, op.record, refreshed)
        elif t is OperationType.CREATE_ALBUM:
            self.api.create_album(op.album, op.album_name)
        elif t is OperationType.DELETE_ALBUM:
            self.api.delete_album(op.album)
        elif t is OperationType.RENAME_ALBUM:
            self.api.rename_album(op.album, op.album_name)
        else:
            raise SyncError(f"unknown operation type: {t}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Photolab SyncClient: sync the local photo tree with the SyncServer.")
    parser.add_argument("--root", required=True, type=Path,
                        help="local photo tree root (the 7-level layout)")
    parser.add_argument("--server-url", required=True,
                        help="SyncServer base URL, e.g. http://127.0.0.1:8123")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="collect/status poll interval in seconds")
    parser.add_argument("--collect-timeout", type=float, default=3600.0,
                        help="max seconds to wait for the collection")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = SyncClient(args.root, args.server_url,
                        poll_interval=args.poll_interval,
                        collect_timeout=args.collect_timeout)
    result = client.run()
    print(f"sync complete: {result.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
