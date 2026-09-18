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
import tempfile
from datetime import datetime
from pathlib import Path

from metadata import Metadata
from place import Place

DATA_DIR = Path(__file__).parent / "data"

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


def main():
    tmp_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    dest_dir = tmp_dir / "data"
    shutil.copytree(DATA_DIR, dest_dir)
    print(f"temporary directory: {tmp_dir}")

    ok = True
    try:
        for image in find_images(dest_dir):
            rel_path = image.relative_to(dest_dir)
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
                if not compare_field(field, want, got):
                    ok = False
                print(f"  [{status}] {field}: expected {want!r}, got {got!r}")
    finally:
        if ok:
            shutil.rmtree(tmp_dir)
            print(f"\nall metadata verified, deleted {tmp_dir}")
        else:
            print(f"\nverification FAILED, kept {tmp_dir} for inspection")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
