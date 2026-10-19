"""Tests for the SyncServer's server pre-validation (design-algorithm.md §7.2):
album-membership repair during collect, and empty-album erasure at session end."""

from datetime import datetime
from pathlib import Path

from PIL import Image

from metadata import Metadata
from record import format_identity
from sync_server import PhotolabSyncServer, _collect


class FakeImmich:
    """In-memory stand-in for the parts of ImmichClient that sync_server uses."""

    def __init__(self, buckets):
        self.buckets = buckets
        self.assets: dict[str, bytes] = {}
        self.membership: dict[str, set[str]] = {}
        self.albums: dict[str, dict] = {}
        self.add_calls: list[tuple[str, list[str]]] = []
        self.remove_calls: list[tuple[str, list[str]]] = []
        self.deleted_albums: list[str] = []
        self._next = 0

    def _id(self, prefix):
        self._next += 1
        return f"{prefix}-{self._next:04d}"

    def make_asset(self, data: bytes) -> str:
        asset_id = self._id("ast")
        self.assets[asset_id] = data
        return asset_id

    def make_album(self, name: str, members=()) -> str:
        album_id = self._id("alb")
        self.albums[album_id] = {"id": album_id, "albumName": name}
        for a in members:
            self.membership.setdefault(a, set()).add(album_id)
        return album_id

    # -- the ImmichClient surface -----------------------------------------

    def get_version(self):
        return 3, 0, 3

    def list_month_buckets(self):
        return [(b, len(ids)) for b, ids in sorted(self.buckets.items())]

    def list_asset_ids_in_bucket(self, time_bucket):
        return list(self.buckets.get(time_bucket, []))

    def download_original(self, asset_id, path):
        Path(path).write_bytes(self.assets[asset_id])

    def _dto(self, album_id):
        dto = dict(self.albums[album_id])
        dto["assetCount"] = sum(1 for m in self.membership.values() if album_id in m)
        return dto

    def list_albums(self, asset_id=None):
        if asset_id is None:
            return [self._dto(a) for a in sorted(self.albums)]
        return [self._dto(a) for a in sorted(self.membership.get(asset_id, set()))]

    def create_album(self, name):
        return self._dto(self.make_album(name))

    def delete_album(self, album_id):
        self.deleted_albums.append(album_id)
        del self.albums[album_id]
        for m in self.membership.values():
            m.discard(album_id)

    def add_assets_to_album(self, album_id, asset_ids):
        self.add_calls.append((album_id, list(asset_ids)))
        for a in asset_ids:
            self.membership.setdefault(a, set()).add(album_id)

    def remove_assets_from_album(self, album_id, asset_ids):
        self.remove_calls.append((album_id, list(asset_ids)))
        for a in asset_ids:
            self.membership.get(a, set()).discard(album_id)


def asset_data(tmp_path, color, *, year=None, month=None, event=None, subevent=None) -> bytes:
    """A unique-pixel JPEG; when event is given, the managed metadata is written in."""
    path = tmp_path / f"asset-{color[0]}{color[1]}{color[2]}.jpg"
    Image.new("RGB", (4, 4), color).save(path, "JPEG")
    if event is not None:
        md = Metadata(date_time_original=datetime(year, month, 1),
                      event=event, subevent=subevent,
                      category="General", supplemental_category="Impersonal", rating=3)
        md.write_to_file(path, write_date_time=True)
    return path.read_bytes()


def run_collect(fake):
    return _collect(fake, on_progress=lambda p: None)


# -- collect: album-membership repair (§7.2) -------------------------------------

def test_collect_keeps_correct_membership(tmp_path):
    a1 = (2022, 7, "E", "S1")
    a2 = (2022, 8, "E", "S2")
    fake = FakeImmich({})
    x1 = fake.make_asset(asset_data(tmp_path, (10, 20, 30), **{
        "year": a1[0], "month": a1[1], "event": a1[2], "subevent": a1[3]}))
    x2 = fake.make_asset(asset_data(tmp_path, (40, 50, 60), **{
        "year": a2[0], "month": a2[1], "event": a2[2], "subevent": a2[3]}))
    fake.buckets = {"2022-07": [x1], "2022-08": [x2]}
    alb1 = fake.make_album("E — S1", [x1])
    alb2 = fake.make_album("E — S2", [x2])

    _, _, _, identity_to_album = run_collect(fake)

    assert identity_to_album == {format_identity(a1): alb1, format_identity(a2): alb2}
    assert fake.add_calls == []
    assert fake.remove_calls == []
    assert set(fake.albums) == {alb1, alb2}


def test_collect_moves_misfiled_asset_to_existing_album(tmp_path):
    s1 = (2022, 7, "E", "S1")
    s2 = (2022, 7, "E", "S2")
    fake = FakeImmich({})
    x1 = fake.make_asset(asset_data(tmp_path, (10, 20, 30), **{
        "year": s1[0], "month": s1[1], "event": s1[2], "subevent": s1[3]}))
    x2 = fake.make_asset(asset_data(tmp_path, (40, 50, 60), **{
        "year": s1[0], "month": s1[1], "event": s1[2], "subevent": s1[3]}))
    x3 = fake.make_asset(asset_data(tmp_path, (70, 80, 90), **{
        "year": s2[0], "month": s2[1], "event": s2[2], "subevent": s2[3]}))
    x4 = fake.make_asset(asset_data(tmp_path, (100, 110, 120), **{
        "year": s2[0], "month": s2[1], "event": s2[2], "subevent": s2[3]}))
    fake.buckets = {"2022-07": [x1, x2, x3, x4]}
    # x1 (identity S1) is misfiled in the S2 album
    alb1 = fake.make_album("E — S1", [x2])
    alb2 = fake.make_album("E — S2", [x1, x3, x4])

    _, _, _, identity_to_album = run_collect(fake)

    assert identity_to_album == {format_identity(s1): alb1, format_identity(s2): alb2}
    assert fake.membership[x1] == {alb1}
    assert fake.membership[x2] == {alb1}
    assert fake.membership[x3] == {alb2}
    assert fake.membership[x4] == {alb2}
    assert (alb1, [x1]) in fake.add_calls
    assert (alb2, [x1]) in fake.remove_calls
    assert set(fake.albums) == {alb1, alb2}  # nothing created


def test_collect_creates_missing_albums(tmp_path):
    s1 = (2022, 7, "E", "S1")
    misc = (2010, 6, "Misc", "X")
    fake = FakeImmich({"2022-07": [], "2010-06": []})
    x1 = fake.make_asset(asset_data(tmp_path, (10, 20, 30), **{
        "year": s1[0], "month": s1[1], "event": s1[2], "subevent": s1[3]}))
    x2 = fake.make_asset(asset_data(tmp_path, (40, 50, 60), **{
        "year": misc[0], "month": misc[1], "event": misc[2], "subevent": misc[3]}))
    fake.buckets = {"2022-07": [x1], "2010-06": [x2]}
    # both assets are in no album at all

    _, _, _, identity_to_album = run_collect(fake)

    by_name = {a["albumName"]: a["id"] for a in fake.albums.values()}
    assert set(by_name) == {"E — S1", "X"}  # the Misc album is named after the subevent alone
    assert identity_to_album == {format_identity(s1): by_name["E — S1"],
                                 format_identity(misc): by_name["X"]}
    assert fake.membership[x1] == {by_name["E — S1"]}
    assert fake.membership[x2] == {by_name["X"]}


def test_collect_converges_duplicate_identity_albums(tmp_path):
    s1 = (2022, 7, "E", "S1")
    fake = FakeImmich({"2022-07": []})
    assets = []
    for color in ((10, 20, 30), (40, 50, 60), (70, 80, 90)):
        assets.append(fake.make_asset(asset_data(tmp_path, color, **{
            "year": s1[0], "month": s1[1], "event": s1[2], "subevent": s1[3]})))
    fake.buckets = {"2022-07": assets}
    # the same identity lives in two albums: 2 members vs 1 member
    alb_a = fake.make_album("E — S1", assets[:2])
    alb_b = fake.make_album("E — S1 (copy)", [assets[2]])

    _, _, _, identity_to_album = run_collect(fake)

    assert identity_to_album == {format_identity(s1): alb_a}
    assert fake.membership[assets[2]] == {alb_a}
    assert (alb_b, [assets[2]]) in fake.remove_calls
    # the loser album is left empty; session end erases it
    assert fake._dto(alb_b)["assetCount"] == 0


def test_collect_leaves_incomplete_record_assets_alone(tmp_path):
    s1 = (2022, 7, "E", "S1")
    fake = FakeImmich({"2022-07": []})
    x1 = fake.make_asset(asset_data(tmp_path, (10, 20, 30)))  # no metadata at all
    x2 = fake.make_asset(asset_data(tmp_path, (40, 50, 60), **{
        "year": s1[0], "month": s1[1], "event": s1[2], "subevent": s1[3]}))
    fake.buckets = {"2022-07": [x1, x2]}
    # both sit in one mixed album; the complete record anchors it to S1
    alb = fake.make_album("E — S1", [x1, x2])

    _, _, _, identity_to_album = run_collect(fake)

    assert identity_to_album == {format_identity(s1): alb}
    assert fake.membership[x1] == {alb}  # untouched: a MOVE will place it later
    assert fake.add_calls == []
    assert fake.remove_calls == []


# -- session end: empty-album erasure (§7.2) ---------------------------------------

def test_erase_empty_albums_deletes_only_the_empty_ones(tmp_path):
    fake = FakeImmich({"2022-07": []})
    x1 = fake.make_asset(asset_data(tmp_path, (10, 20, 30), **{
        "year": 2022, "month": 7, "event": "E", "subevent": "S1"}))
    fake.buckets = {"2022-07": [x1]}
    kept = fake.make_album("E — S1", [x1])
    empty1 = fake.make_album("Scratch")
    empty2 = fake.make_album("E — S2")  # created but never filled

    PhotolabSyncServer(fake)._erase_empty_albums()  # type: ignore[arg-type]

    assert fake.deleted_albums == sorted([empty1, empty2])
    assert set(fake.albums) == {kept}
