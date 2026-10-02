"""Operation builder for the SyncClient engine (see docs/design-algorithm.md).

This module is part of the **SyncClient** (the smart side). Given the local
structure and the remote structure collected from SyncServer, it produces the
complete, **ordered** list of atomic operations that SyncServer (a dumb
translator) must apply to make the remote side match the local side.

The algorithm is pure: it takes two maps and returns an ordered list. No side
effects, no network access (design-algorithm.md §1).

Inputs (design-algorithm.md §1.1):

    local_pairs:  { content_sha512_hex : Record }
    remote_pairs: { content_sha512_hex : Record }

The key is the photo's content hash (SHA512 over the raw file bytes — pixels,
container, and metadata are ALL part of the hash). The value is the full
**Record** (design-algorithm.md §1.2): place + full date_time_original. A file
change (pixels, metadata, or encoding) yields a new hash, so it is a
DELETE(old) + UPLOAD(new) — a MOVE fires only when byte-identical files carry
different records (§4).

`extra_copies` (design-api.md §6.2 "duplicate encoding") lists the additional
server-side assets sharing a hash: [{"asset_id": ..., "hash": ...}]; each gets
a targeted DELETE (§5.2).

Design constraints the produced plan must satisfy (design-algorithm.md §6.1):
  * ordering is DELETE, RENAME_ALBUM, CREATE_ALBUM, MOVE, DELETE_ALBUM, UPLOAD
    — every operation's preconditions hold when it runs;
  * the plan is idempotent, so a crashed run is completed by simply re-running
    the sync (§6.2);
  * remote-only photos are deleted first (the local tree is the source of
    truth, §5.2).

Pre-validation of the local set (duplicate content, §7) is done by the
SyncClient before any session is started; it operates on the raw (hash,
Record) list because a dict keyed by hash cannot represent a duplicate.
"""

from dataclasses import dataclass, field
from enum import Enum

import Levenshtein

from record import EM_DASH, Record

# Section numbers below (e.g. §5.1) refer to docs/design-algorithm.md.


class OperationType(Enum):
    UPLOAD = "upload"
    DELETE = "delete"
    MOVE = "move"
    CREATE_ALBUM = "create_album"
    DELETE_ALBUM = "delete_album"
    RENAME_ALBUM = "rename_album"


class LocalValidationError(Exception):
    """Fatal pre-check failure: the local set is invalid, the sync must not start (§7)."""


@dataclass
class Operation:
    """One atomic operation SyncServer must apply (design-api.md §6.3).

    Field usage per type:
        UPLOAD        hash, record (target; the file bytes are resolved by the
                      client from its in-memory final files, keyed by hash)
        DELETE        hash — or asset_id for a server-side duplicate copy (§5.2)
        MOVE          hash, from_record (current remote record), record (target)
        CREATE_ALBUM  album (4-level identity), album_name
        DELETE_ALBUM  album
        RENAME_ALBUM  album (current identity), album_name (new display name)

    A MOVE is emitted when byte-identical files (same content hash) carry
    different records (§4). A file change (pixels, metadata, or encoding) yields
    a new hash and is a DELETE(old) + UPLOAD(new), not a MOVE. The MOVE payload
    carries the refreshed file bytes (the client's final file) so the server can
    update the asset's stored metadata.
    """

    operation_type: OperationType
    hash: str | None = None
    asset_id: str | None = None
    record: Record | None = None
    from_record: Record | None = None
    album: tuple | None = None
    album_name: str | None = None

    def __str__(self) -> str:
        h = self.hash
        hshort = f"{h[:12]}…" if h else (self.asset_id or "?")
        if self.operation_type is OperationType.UPLOAD:
            return f"UPLOAD {hshort} -> {self.record}"
        if self.operation_type is OperationType.DELETE:
            return f"DELETE {hshort}"
        if self.operation_type is OperationType.MOVE:
            return f"MOVE {hshort} {self.from_record} -> {self.record}"
        if self.operation_type is OperationType.CREATE_ALBUM:
            return f"CREATE_ALBUM {self.album} ({self.album_name})"
        if self.operation_type is OperationType.DELETE_ALBUM:
            return f"DELETE_ALBUM {self.album}"
        if self.operation_type is OperationType.RENAME_ALBUM:
            return f"RENAME_ALBUM {self.album} -> {self.album_name}"
        return f"OP {self.operation_type} {hshort}"


def validate_local_pairs(pairs: list[tuple[str, Record]]) -> None:
    """Fatal pre-check on the local set (design-algorithm.md §7).

    `pairs` is the raw scan result: one (hash, record) per local image, BEFORE
    deduplication into a dict. A single content hash MUST map to exactly one
    record. If the same hash appears more than once — under a different record
    (a photo in two events) or even under the same record (a plain duplicate
    file) — this is FATAL and the sync does not start.

    Server-side duplicates are NOT a pre-check (§5.2): they are resolved
    during the plan, so a server-side duplicate must not by itself abort a
    sync.
    """
    seen: dict[str, Record] = {}
    for h, record in pairs:
        if h in seen:
            if seen[h] == record:
                raise LocalValidationError(
                    f"duplicate content: content hash {h[:16]}… appears twice with the "
                    f"same record ({record}) — duplicates are not tolerated"
                )
            raise LocalValidationError(
                f"duplicate content: content hash {h[:16]}… is in two events: "
                f"{seen[h]} and {record}"
            )
        seen[h] = record


def build_sync_algorithm(
    local_pairs: dict[str, Record],
    remote_pairs: dict[str, Record],
    extra_copies: list[dict] | None = None,
) -> list[Operation]:
    """Build the ordered list of atomic operations for one sync run.

    local_pairs / remote_pairs: hash -> Record (design-algorithm.md §1.1-1.2).
    extra_copies: server-side duplicate assets [{"asset_id", "hash"}] reported
    by the collect (design-api.md §6.2); each yields a targeted DELETE.

    Returns operations ordered as DELETE, RENAME_ALBUM, CREATE_ALBUM, MOVE,
    DELETE_ALBUM, UPLOAD (§6.1). Pure: no side effects, no network access.
    """
    extra_copies = extra_copies or []

    # --- §5.2 removed content -> DELETE (first; the client is authoritative) ---
    deletions = [
        # Targeted by the server-side asset_id, not the hash: the hash maps
        # to the primary (kept) copy, the extra one is addressed by id (§5.2).
        Operation(OperationType.DELETE, asset_id=copy["asset_id"])
        for copy in extra_copies
    ]
    for h in sorted(set(remote_pairs) - set(local_pairs)):
        deletions.append(Operation(OperationType.DELETE, hash=h))

    # --- §5.1 new content -> UPLOAD ---
    uploads = [
        Operation(OperationType.UPLOAD, hash=h, record=local_pairs[h])
        for h in sorted(set(local_pairs) - set(remote_pairs))
    ]

    # --- §5.3 moved / metadata-changed -> MOVE ---
    common = set(local_pairs) & set(remote_pairs)
    moves = [
        Operation(OperationType.MOVE, hash=h, from_record=remote_pairs[h], record=local_pairs[h])
        for h in sorted(common)
        if local_pairs[h] != remote_pairs[h]
    ]

    # --- §3.2 album create / rename / delete ---
    local_albums = {r.album_identity for r in local_pairs.values() if r.album_identity}
    remote_albums = {r.album_identity for r in remote_pairs.values() if r.album_identity}

    # Reverse index: hash -> local album identity (O(1) lookup of where a
    # remote album's pictures ended up).
    local_album_by_hash = {
        h: r.album_identity for h, r in local_pairs.items() if r.album_identity
    }

    # RENAME candidates: for each remote identity missing locally, the local
    # albums containing its former pictures are candidates, scored by the
    # similarity ratio of the renameable part — the display name
    # (event — subevent), not the full 4-level identity.
    #
    # A rename only changes the event/subevent part; the server keeps the
    # source identity's year/month and updates its identity mapping
    # (design-api.md §6.3). So a candidate is usable only if it shares the
    # source's year/month (a pure year/month change is NOT a rename — it is
    # create + delete, §3.2) and its identity is free on the remote side
    # (renaming into an existing album would collide).
    rename_candidates = []
    for missing in sorted(remote_albums - local_albums):
        names = _album_name(remote_pairs, missing)
        candidate_albums = set()
        for h, r in remote_pairs.items():
            if r.album_identity == missing and h in local_album_by_hash:
                candidate_albums.add(local_album_by_hash[h])
        for candidate in candidate_albums:
            if candidate in remote_albums:
                continue
            if (candidate[0], candidate[1]) != (missing[0], missing[1]):
                continue
            ratio = Levenshtein.ratio(_display_name(*candidate[2:]), names)
            rename_candidates.append((ratio, missing, candidate))

    # Greedy best-match: best ratio first; no album is renamed twice and no two
    # albums are renamed into the same target. The names break ties stably.
    rename_candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    album_renames: dict[tuple, tuple] = {}   # source identity -> target identity
    taken_target_ids: set[tuple] = set()
    for ratio, source, target in rename_candidates:
        if source in album_renames or target in taken_target_ids:
            continue
        album_renames[source] = target
        taken_target_ids.add(target)

    rename_ops = [
        Operation(OperationType.RENAME_ALBUM, album=source,
                  album_name=_display_name(*album_renames[source][2:]))
        for source in sorted(album_renames)
    ]

    # CREATE: a local identity not in remote_albums and not a rename target
    # (a rename target exists remotely under its old identity — creating it
    # again would yield a duplicate album with the same display name).
    rename_targets = set(album_renames.values())
    create_ops = [
        Operation(OperationType.CREATE_ALBUM, album=identity,
                  album_name=_display_name(*identity[2:]))
        for identity in sorted(local_albums - remote_albums - rename_targets)
    ]

    # DELETE_ALBUM: a remote identity missing locally and not renamed, emitted
    # after its surviving photos have been moved out (moves run before
    # deletions, §6.1). Empty remote albums are kept as-is (§8).
    delete_album_ops = [
        Operation(OperationType.DELETE_ALBUM, album=identity)
        for identity in sorted(remote_albums - local_albums - set(album_renames))
    ]

    # The order matters (part of the contract, §6.1): photos are deleted first,
    # albums are renamed and created before photos are moved into them, and an
    # old album is deleted only after its surviving photos have been moved out.
    return deletions + rename_ops + create_ops + moves + delete_album_ops + uploads


def _album_name(remote_pairs: dict[str, Record], identity: tuple) -> str:
    """The display name of a remote album, from any of its records."""
    for r in remote_pairs.values():
        if r.album_identity == identity:
            return _display_name(r.event, r.subevent)
    return _display_name(identity[2], identity[3])


def _display_name(event, subevent) -> str:
    return f"{event or ''}{EM_DASH}{subevent or ''}".strip()
