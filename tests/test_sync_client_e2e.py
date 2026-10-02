"""End-to-end test: SyncClient against TestStubSyncServer.

Builds a temporary 7-level tree of real JPEGs (some with a pre-existing
DateTimeOriginal, some without), runs a full sync against the stub (which
reports an empty remote set), and checks:

- the plan is a pure upload and every upload reaches the server
- the uploaded records match the path + the date-time rule (design-metadata.md)
- the local files are written back with the augmented metadata, pixels intact
- the endpoint call sequence matches the session flow (design-api.md)
"""

from datetime import datetime
from pathlib import Path

import pyexiv2
from PIL import Image

from content_hash import compute_content_hash
from metadata import Metadata
from stub_sync_server import TestStubSyncServer
from sync_client import OperationType, SyncClient

A = "2026/april/Beach/Sunset/General/Impersonal/4 stars"
B = "2026/may/City/Night/General/Impersonal/5 stars"


def _make_jpeg(path: Path, color, date_time: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (12, 9), color).save(path, "JPEG")
    if date_time:
        # Write DateTimeOriginal via pyexiv2 into the canonical Exif sub-IFD
        # (Exif.Photo), matching real camera files and what Metadata.from_file reads.
        with pyexiv2.Image(str(path)) as ex:
            ex.modify_exif({"Exif.Photo.DateTimeOriginal": date_time})


def _tree(root: Path) -> dict[str, str]:
    _make_jpeg(root / A / "a.jpg", (200, 30, 30), "2026:04:05 10:20:30")
    _make_jpeg(root / A / "b.jpg", (30, 200, 30), None)
    _make_jpeg(root / B / "c.jpg", (30, 30, 200), "2025:01:31 08:15:00")
    (root / "not-in-the-tree.txt").write_text("ignored")
    return {
        "a": str((root / A / "a.jpg").relative_to(root)),
        "b": str((root / A / "b.jpg").relative_to(root)),
        "c": str((root / B / "c.jpg").relative_to(root)),
    }


def test_full_sync_against_empty_stub(tmp_path):
    root = tmp_path / "photos"
    rel = _tree(root)
    # Content hashing changes the file bytes (metadata is written back), so
    # "pixels intact" is checked by comparing decoded RGB, not file hashes.
    pixels_before = {name: Image.open(root / relpath).convert("RGB").tobytes()
                     for name, relpath in rel.items()}

    with TestStubSyncServer() as server:
        client = SyncClient(root, server.url, poll_interval=0)
        result = client.run()

    # --- the plan: two album creates + three uploads (empty remote set) ------
    # the two april photos share one album; may is a second album (§3.2 CREATE_ALBUM)
    assert result.counts.get(OperationType.UPLOAD) == 3
    assert result.counts.get(OperationType.CREATE_ALBUM) == 2
    assert sum(result.counts.values()) == 5
    assert result.remote_photos == 0

    # --- every upload reached the server with the right record ----------------
    uploads = server.state.uploads
    assert len(uploads) == 3

    # the stub keys uploads by the content hash of the FINAL (augmented) file;
    # after the sync the local files ARE the augmented files, so their hashes
    # are the upload keys (the two april photos share one album, so the album
    # cannot be used as a key).
    a_up = uploads[compute_content_hash((root / rel["a"]).read_bytes())]
    b_up = uploads[compute_content_hash((root / rel["b"]).read_bytes())]
    c_up = uploads[compute_content_hash((root / rel["c"]).read_bytes())]

    assert a_up["record"] == {
        "year": 2026, "month": 4, "event": "Beach", "subevent": "Sunset",
        "category": "General", "supplemental_category": "Impersonal",
        "rating": 4, "date_time_original": "2026-04-05T10:20:30",
    }
    # b.jpg had no original date-time -> fallback year/month/01 00:00:00
    assert b_up["record"] == {
        "year": 2026, "month": 4, "event": "Beach", "subevent": "Sunset",
        "category": "General", "supplemental_category": "Impersonal",
        "rating": 4, "date_time_original": "2026-04-01T00:00:00",
    }
    assert c_up["record"] == {
        "year": 2026, "month": 5, "event": "City", "subevent": "Night",
        "category": "General", "supplemental_category": "Impersonal",
        "rating": 5, "date_time_original": "2026-05-31T08:15:00",
    }
    # both april photos live in the same album; may is a second album
    assert a_up["album"] == b_up["album"] == "2026/4/Beach/Sunset"
    assert c_up["album"] == "2026/5/City/Night"
    for up in uploads.values():
        assert up["content_type"] == "image/jpeg"
        assert up["file_size"] > 0

    # --- the local files now carry the augmented metadata, pixels intact ------
    md_a = Metadata.from_file(root / rel["a"])
    assert (md_a.event, md_a.subevent) == ("Beach", "Sunset")
    assert (md_a.category, md_a.supplemental_category, md_a.rating) == ("General", "Impersonal", 4)
    # date-time rule: day+time from the file, year/month from the path
    assert md_a.date_time_original == datetime(2026, 4, 5, 10, 20, 30)

    md_b = Metadata.from_file(root / rel["b"])
    # no original date-time -> fallback year/month/01 00:00:00
    assert md_b.date_time_original == datetime(2026, 4, 1, 0, 0, 0)

    md_c = Metadata.from_file(root / rel["c"])
    # day 31 is valid in may; year/month replaced by the path's
    assert md_c.date_time_original == datetime(2026, 5, 31, 8, 15, 0)

    for name, r in rel.items():
        assert Image.open(root / r).convert("RGB").tobytes() == pixels_before[name]

    # no temp files were left behind
    assert list(root.rglob("*.tmp")) == []


def test_session_call_sequence(tmp_path):
    root = tmp_path / "photos"
    _tree(root)

    with TestStubSyncServer() as server:
        server.set_collect_running_polls(0)  # collect reports done immediately
        SyncClient(root, server.url, poll_interval=0).run()

    paths = [c["path"] for c in server.state.requests_log]
    assert paths == [
        "/v1/sync/start",
        "/v1/collect",
        "/v1/collect/status",
        # CREATE_ALBUM precedes UPLOAD (§6.1); two distinct albums
        "/v1/album/create", "/v1/album/create",
        "/v1/upload", "/v1/upload", "/v1/upload",
        "/v1/sync/stop",
    ]
    assert all(c["status"] in (202, 200) for c in server.state.requests_log)
