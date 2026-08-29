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
from pathlib import Path

import pyexiv2

DATA_DIR = Path(__file__).parent / "data"

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp",
    ".heic", ".heif", ".cr2", ".dng", ".arw", ".nef", ".rw2", ".orf",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

EM_DASH = " \u2014 "


def parse_path_parts(rel_path: Path):
    parts = rel_path.parts[:-1]
    if len(parts) != 7:
        raise ValueError(f"expected 7 directory levels, got {len(parts)}: {rel_path}")
    year_s, month_s, event, subevent, category, supplemental, rating_s = parts
    year = int(year_s)
    month = MONTHS[month_s.lower()]
    rating = int(rating_s.split()[0])
    return year, month, event, subevent, category, supplemental, rating


def expected_tags(year, month, event, subevent, category, supplemental, rating):
    return {
        "Exif.Photo.DateTimeOriginal": f"{year:04d}:{month:02d}:01 00:00:00",
        "Xmp.iptcExt.Event": f"{event}{EM_DASH}{subevent}",
        "Xmp.xmp.Rating": str(rating),
        "Xmp.photoshop.Category": category,
        "Xmp.photoshop.SupplementalCategories": [supplemental],
        "Iptc.Application2.Category": category,
        "Iptc.Application2.SuppCategory": [supplemental],
    }


def write_metadata(image_path: Path, year, month, event, subevent, category, supplemental, rating):
    with pyexiv2.Image(str(image_path)) as img:
        img.modify_exif({"Exif.Photo.DateTimeOriginal": f"{year:04d}:{month:02d}:01 00:00:00"})
        img.modify_xmp({
            "Xmp.Iptc4xmpExt.Event": f"{event}{EM_DASH}{subevent}",
            "Xmp.xmp.Rating": str(rating),
            "Xmp.photoshop.Category": category,
            "Xmp.photoshop.SupplementalCategories": [supplemental],
        })
        img.modify_iptc({
            "Iptc.Application2.Category": category,
            "Iptc.Application2.SuppCategory": [supplemental],
        })


def read_metadata(image_path: Path):
    with pyexiv2.Image(str(image_path)) as img:
        return {
            **img.read_exif(),
            **img.read_xmp(),
            **img.read_iptc(),
        }


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
            year, month, event, subevent, category, supplemental, rating = parse_path_parts(rel_path)
            write_metadata(image, year, month, event, subevent, category, supplemental, rating)

            expected = expected_tags(year, month, event, subevent, category, supplemental, rating)
            actual = read_metadata(image)
            print(f"\n{rel_path}")
            for tag, want in expected.items():
                got = actual.get(tag)
                status = "OK" if got == want else "MISMATCH"
                if got != want:
                    ok = False
                print(f"  [{status}] {tag}: expected {want!r}, got {got!r}")
    finally:
        if ok:
            shutil.rmtree(tmp_dir)
            print(f"\nall metadata verified, deleted {tmp_dir}")
        else:
            print(f"\nverification FAILED, kept {tmp_dir} for inspection")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
