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

EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"

FIELDS = (
    "date_time_original",
    "event",
    "subevent",
    "category",
    "supplemental_category",
    "rating",
)


class Metadata:
    def __init__(self,
                 date_time_original: datetime | None = None,
                 event: str | None = None,
                 subevent: str | None = None,
                 category: str | None = None,
                 supplemental_category: str | None = None,
                 rating: int | None = None):
        self.date_time_original = date_time_original
        self.event = event
        self.subevent = subevent
        self.category = category
        self.supplemental_category = supplemental_category
        self.rating = rating

    @classmethod
    def from_file(cls, file_path: Path | str) -> "Metadata":
        with pyexiv2.Image(str(file_path)) as img:
            exif = img.read_exif()
            xmp = img.read_xmp()
            iptc = img.read_iptc()

        date_time_original = None
        value = exif.get("Exif.Photo.DateTimeOriginal")
        if value:
            date_time_original = datetime.strptime(value, EXIF_DATE_FORMAT)

        event = None
        subevent = None
        value = xmp.get("Xmp.iptcExt.Event")
        if value:
            event, sep, sub = value.partition(EM_DASH)
            if sep:
                subevent = sub

        category = xmp.get("Xmp.photoshop.Category") or iptc.get("Iptc.Application2.Category")

        value = xmp.get("Xmp.photoshop.SupplementalCategories") or iptc.get("Iptc.Application2.SuppCategory")
        supplemental_category = value[0] if value else None

        rating = None
        value = xmp.get("Xmp.xmp.Rating")
        if value is not None:
            rating = int(value)

        return cls(date_time_original, event, subevent, category, supplemental_category, rating)

    @classmethod
    def from_path_string(cls, file_path: Path | str) -> "Metadata":
        parts = Path(file_path).parts[:-1]
        if len(parts) != 7:
            raise ValueError(f"expected 7 directory levels, got {len(parts)}: {file_path}")
        year_s, month_s, event, subevent, category, supplemental_category, rating_s = parts
        return cls(
            date_time_original=datetime(int(year_s), MONTHS[month_s.lower()], 1),
            event=event,
            subevent=subevent,
            category=category,
            supplemental_category=supplemental_category,
            rating=int(rating_s.split()[0]),
        )

    def write_to_file(self, file_path: Path | str, write_date_time: bool = False):
        with pyexiv2.Image(str(file_path)) as img:
            if write_date_time:
                img.modify_exif({
                    "Exif.Photo.DateTimeOriginal": self.date_time_original.strftime(EXIF_DATE_FORMAT),
                })
            img.modify_xmp({
                "Xmp.Iptc4xmpExt.Event": f"{self.event}{EM_DASH}{self.subevent}",
                "Xmp.xmp.Rating": str(self.rating),
                "Xmp.photoshop.Category": self.category,
                "Xmp.photoshop.SupplementalCategories": [self.supplemental_category],
            })
            img.modify_iptc({
                "Iptc.Application2.Category": self.category,
                "Iptc.Application2.SuppCategory": [self.supplemental_category],
            })


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
            expected = Metadata.from_path_string(rel_path)

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
