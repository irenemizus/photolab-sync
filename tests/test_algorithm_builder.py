"""Tests for algorithm_builder.build_sync_algorithm (docs/design-algorithm.md)."""

from datetime import datetime

import pytest

from algorithm_builder import (
    LocalValidationError,
    Operation,
    OperationType,
    build_sync_algorithm,
    validate_local_pairs,
)
from record import Record


def rec(year, month, event, subevent, category="c", supp="s", rating=3,
        dtp: datetime | None = None) -> Record:
    return Record(year, month, event, subevent, category, supp, rating, dtp)


def A(y, m, e, s):
    return (y, m, e, s)


def types(ops):
    return [op.operation_type for op in ops]


def by_type(ops, t):
    return [op for op in ops if op.operation_type is t]


# --- pre-validation (§7) --------------------------------------------------------

def test_validate_rejects_same_hash_two_records():
    r1 = rec(2026, 4, "A", "B")
    r2 = rec(2026, 4, "A", "C")
    with pytest.raises(LocalValidationError):
        validate_local_pairs([("h1", r1), ("h1", r2)])


def test_validate_rejects_plain_duplicate():
    r1 = rec(2026, 4, "A", "B")
    with pytest.raises(LocalValidationError):
        validate_local_pairs([("h1", r1), ("h1", r1)])


def test_validate_accepts_distinct_hashes():
    validate_local_pairs([("h1", rec(2026, 4, "A", "B")),
                          ("h2", rec(2026, 4, "A", "C"))])


# --- basic phases (§5) ----------------------------------------------------------

def test_noop_when_identical():
    r = rec(2026, 4, "A", "B", dtp=datetime(2026, 4, 5, 10, 0, 0))
    assert build_sync_algorithm({"h": r}, {"h": r}) == []


def test_upload_for_new_content():
    # a new photo in a new album: the album is created first, then the upload
    # (§3.2 CREATE_ALBUM, §6.1 ordering puts CREATE_ALBUM before UPLOAD)
    local = {"h1": rec(2026, 4, "A", "B")}
    ops = build_sync_algorithm(local, {})
    assert types(ops) == [OperationType.CREATE_ALBUM, OperationType.UPLOAD]
    upload = by_type(ops, OperationType.UPLOAD)[0]
    assert upload.hash == "h1"
    assert upload.record == local["h1"]


def test_delete_for_removed_content():
    remote = {"h1": rec(2026, 4, "A", "B")}
    ops = build_sync_algorithm({}, remote)
    # the photo is deleted; its album (now empty, not renamed) is deleted too
    assert types(ops) == [OperationType.DELETE, OperationType.DELETE_ALBUM]
    assert ops[0].hash == "h1"
    assert ops[1].album == A(2026, 4, "A", "B")


def test_pixel_change_is_delete_plus_upload_not_move():
    # different pixel bit-stream => different hash: old one gone, new one added (§4)
    local = {"h2": rec(2026, 4, "A", "B")}
    remote = {"h1": rec(2026, 4, "A", "B")}
    ops = build_sync_algorithm(local, remote)
    assert types(ops) == [OperationType.DELETE, OperationType.UPLOAD]


def test_metadata_only_change_is_move():
    # same pixel bit-stream (same hash), rating changed -> MOVE (§4)
    r_local = rec(2026, 4, "A", "B", rating=5)
    r_remote = rec(2026, 4, "A", "B", rating=3)
    ops = build_sync_algorithm({"h": r_local}, {"h": r_remote})
    assert types(ops) == [OperationType.MOVE]
    op = ops[0]
    assert op.from_record == r_remote
    assert op.record == r_local


def test_day_time_change_within_same_month_is_move():
    # invisible in the path, visible in the record (§1.3)
    r_local = rec(2026, 4, "A", "B", dtp=datetime(2026, 4, 5, 10, 0, 0))
    r_remote = rec(2026, 4, "A", "B", dtp=datetime(2026, 4, 9, 10, 0, 0))
    ops = build_sync_algorithm({"h": r_local}, {"h": r_remote})
    assert types(ops) == [OperationType.MOVE]


def test_extra_copies_yield_targeted_delete():
    # server-side duplicate: keep the primary, delete the extra by asset_id (§5.2)
    r = rec(2026, 4, "A", "B")
    ops = build_sync_algorithm({"h": r}, {"h": r},
                               extra_copies=[{"asset_id": "dup-1", "hash": "h"}])
    assert types(ops) == [OperationType.DELETE]
    assert ops[0].asset_id == "dup-1"
    assert ops[0].hash is None


# --- albums (§3) ------------------------------------------------------------------

def test_rename_on_similar_display_name():
    # a typo in the subevent: rename the album, move the photo to its record
    r_local = rec(2026, 4, "A", "Oane")
    r_remote = rec(2026, 4, "A", "One")
    ops = build_sync_algorithm({"h": r_local}, {"h": r_remote})
    assert set(types(ops)) == {OperationType.RENAME_ALBUM, OperationType.MOVE}
    rename = by_type(ops, OperationType.RENAME_ALBUM)[0]
    assert rename.album == A(2026, 4, "A", "One")
    assert rename.album_name == "A — Oane"
    # rename runs before the move (§6.1)
    assert types(ops).index(OperationType.RENAME_ALBUM) < types(ops).index(OperationType.MOVE)


def test_pure_year_month_change_is_create_plus_delete_not_rename():
    # a rename cannot change year/month (design-api.md §6.3): this is Case A
    # — the new (year,month) is its own album (§3.2/§3.3)
    r_local = rec(2026, 5, "A", "B")
    r_remote = rec(2026, 4, "A", "B")
    ops = build_sync_algorithm({"h": r_local}, {"h": r_remote})
    assert types(ops) == [
        OperationType.CREATE_ALBUM,
        OperationType.MOVE,
        OperationType.DELETE_ALBUM,
    ]
    assert by_type(ops, OperationType.CREATE_ALBUM)[0].album == A(2026, 5, "A", "B")
    assert by_type(ops, OperationType.DELETE_ALBUM)[0].album == A(2026, 4, "A", "B")


def test_create_album_for_new_local_album():
    ops = build_sync_algorithm({"h": rec(2026, 4, "A", "B")}, {})
    assert types(ops) == [OperationType.CREATE_ALBUM, OperationType.UPLOAD]
    create = by_type(ops, OperationType.CREATE_ALBUM)[0]
    assert create.album == A(2026, 4, "A", "B")
    assert create.album_name == "A — B"


def test_same_display_name_different_year_month_are_two_albums():
    # Case A: same event, two years -> two albums, both kept (§3.1, §3.3)
    local = {
        "h1": rec(2025, 12, "X", "Y"),
        "h2": rec(2026, 1, "X", "Y"),
    }
    ops = build_sync_algorithm(local, {})
    created = sorted(op.album for op in by_type(ops, OperationType.CREATE_ALBUM))
    assert created == [A(2025, 12, "X", "Y"), A(2026, 1, "X", "Y")]


def test_rename_target_is_not_created_again():
    # the renamed identity exists remotely under its old name; creating the
    # target identity again would yield a duplicate album
    r_local = rec(2026, 4, "A", "Oane")
    r_remote = rec(2026, 4, "A", "One")
    ops = build_sync_algorithm({"h": r_local}, {"h": r_remote})
    assert by_type(ops, OperationType.CREATE_ALBUM) == []
    assert by_type(ops, OperationType.DELETE_ALBUM) == []


def test_rename_into_existing_identity_is_create_plus_delete():
    # "A — One"'s photos now live in "A — Oane", but that identity already
    # exists remotely -> "A — One" cannot rename into it; it is deleted and
    # its photos are moved (h1 to the existing "A — Oane" album, h2 to a new one).
    local = {"h1": rec(2026, 4, "A", "Oane"), "h2": rec(2026, 4, "B", "C")}
    remote = {"h1": rec(2026, 4, "A", "One"), "h2": rec(2026, 4, "A", "Oane")}
    ops = build_sync_algorithm(local, remote)
    assert by_type(ops, OperationType.RENAME_ALBUM) == []
    deleted = [op.album for op in by_type(ops, OperationType.DELETE_ALBUM)]
    assert A(2026, 4, "A", "One") in deleted


# --- ordering (§6.1) ---------------------------------------------------------------

def test_operation_order_delete_rename_create_move_delete_album_upload():
    local = {
        "new": rec(2026, 4, "N", "N"),
        "renamed": rec(2026, 4, "A", "Oane"),
        "moved": rec(2026, 4, "A", "Oane", rating=5),
    }
    remote = {
        "gone": rec(2026, 4, "G", "G"),
        "renamed": rec(2026, 4, "A", "One"),
        "moved": rec(2026, 4, "A", "One", rating=3),
    }
    ops = build_sync_algorithm(local, remote)
    t = types(ops)
    order = [OperationType.DELETE, OperationType.RENAME_ALBUM, OperationType.CREATE_ALBUM,
             OperationType.MOVE, OperationType.DELETE_ALBUM, OperationType.UPLOAD]
    positions = {ot: 0 for ot in order}
    last = -1
    for ot in t:
        assert positions[ot] >= last, f"ordering violated by {ot} in {t}"
        last = positions[ot]
