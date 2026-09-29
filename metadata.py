"""Metadata model for reading and writing image tags via pyexiv2.

Part of the SyncClient upload-preparation pipeline (see docs/design-metadata.md).
For every local image the client does:
    place = Place.from_path_string(rel_path)   # parse the 7-level layout
    md    = Metadata.from_file(path)           # read whatever the file already has
    md.patch_from_place(place)                 # fill missing fields from the path
    md.write_to_file(...)                       # produce the FINAL file (kept in memory)
The final file's pixels are uploaded to the SyncServer and SHA512-hashed (pixels only);
its metadata is what Immich indexes and displays. The 7-level layout never reaches the
server -- it is flattened into these tags before upload.

Tags (mimicking the PhotoLab reference file):
    date_time_original -> Exif.Photo.DateTimeOriginal
    event/subevent     -> Xmp.iptcExt.Event, joined with an em-dash
    category           -> Iptc.Application2.Category + Xmp.photoshop.Category
    supplemental       -> Iptc.Application2.SuppCategory + Xmp.photoshop.SupplementalCategories
    rating             -> Xmp.xmp.Rating (XMP spec Part 1: -1 or [0..5])

Date-time rule: the path carries only year/month, so date_time_original must be preserved
from the original file (fall back to year/month/01 00:00:00 only when the file has none).
TODO: pin down and test write_to_file's date-time behaviour (it currently only writes the
date-time when write_date_time=True and would fail if date_time_original is None).
"""

from datetime import datetime
from pathlib import Path

import pyexiv2

EM_DASH = " \u2014 "

EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"


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

    def patch_from_place(self, place: "Place"):
        self.event = place.event
        self.subevent = place.subevent
        self.category = place.category
        self.supplemental_category = place.supplemental_category
        self.rating = place.rating

    def write_to_file(self, file_path: Path | str, write_date_time: bool = False):
        with pyexiv2.Image(str(file_path)) as img:
            if write_date_time:
                if self.date_time_original is None:
                    raise ValueError(
                        "date_time_original is not set; the SyncClient's date-time rule "
                        "(docs/design-metadata.md step 4) guarantees it is set before write")
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
