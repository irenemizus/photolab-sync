"""Write metadata to images based on their relative path layout and verify it.

Path layout (7 levels above the file name):
    year/month/event/subevent/category/supplemental category/rating

Tags written (mimicking the PhotoLab reference file):
    year/month   -> Exif.Photo.DateTimeOriginal (day 01, 00:00:00)
    event/subevent -> Xmp.Iptc4xmpExt.Event, joined with an em-dash
    category        -> Iptc.Application2.Category + Xmp.photoshop.Category
    supplemental    -> Iptc.Application2.SuppCategory + Xmp.photoshop.SupplementalCategories
    rating          -> Xmp.xmp.Rating (XMP spec Part 1, XMP namespace: -1 or [0..5])
"""

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from metadata import Metadata
from place import Place

DATA_DIR = Path(__file__).parent.parent / "data"

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp",
    ".heic", ".heif", ".cr2", ".dng", ".arw", ".nef", ".rw2", ".orf",
}

FIELDS = (
    "date_time_original",
    "event",
    "subevent",
    "category",
    "supplemental_category",
    "rating",
)


def compare_field(field: str, want, got):
    if field == "date_time_original":
        want = (want.year, want.month) if want is not None else None
        got = (got.year, got.month) if got is not None else None
    return want == got


def find_images(root: Path):
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


REL_PATHS = [str(p.relative_to(DATA_DIR)) for p in find_images(DATA_DIR)]


@pytest.fixture
def workspace(tmp_path):
    dest = tmp_path / "data"
    shutil.copytree(DATA_DIR, dest)
    return dest


@pytest.mark.parametrize("rel_path", REL_PATHS)
def test_written_metadata_matches_path(workspace, rel_path):
    image = workspace / rel_path
    place = Place.from_path_string(rel_path)
    expected = Metadata(date_time_original=datetime(place.year, place.month, 1))
    expected.patch_from_place(place)

    current = Metadata.from_file(image)
    print(f"\n{rel_path}\nbefore:")
    for field in FIELDS:
        print(f"  {field}: {getattr(current, field)!r}")

    expected.write_to_file(image)

    actual = Metadata.from_file(image)
    print("after:")
    for field in FIELDS:
        want = getattr(expected, field)
        got = getattr(actual, field)
        status = "OK" if compare_field(field, want, got) else "MISMATCH"
        print(f"  [{status}] {field}: expected {want!r}, got {got!r}")
        assert compare_field(field, want, got), (
            f"{field}: expected {want!r}, got {got!r}"
        )
