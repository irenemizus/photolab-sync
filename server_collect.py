"""The collection phase of the SyncServer (design-api.md §6.2).

One full collection: enumerate the whole Immich timeline, download every
original, recompute the content hash over the pixel bit-stream, reconstruct
the record from the embedded metadata, repair the album membership
(design-algorithm.md §7.2), and rebuild the live maps the session works on.
No caching: everything is re-downloaded and re-hashed on every collect.
"""

import logging
import shutil
import tempfile
from pathlib import Path

from content_hash import compute_content_hash
from immich_client import ImmichClient
from metadata import Metadata
from record import AlbumIdentity, Record, display_name, format_identity

log = logging.getLogger("photolab-sync.server")

# Membership changes are batched this many assets per Immich call.
_ALBUM_BATCH = 500


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
                              record_by_asset: dict[str, Record]) -> AlbumIdentity | None:
    """The identity an existing album is anchored to: the majority vote over
    its members' record identities (ties broken by the lexicographically
    smallest identity); None when no member has a resolvable identity."""
    counts: dict[AlbumIdentity, int] = {}
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
            name = display_name(identity.event, identity.subevent)
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
