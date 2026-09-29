"""Place derived from an image's relative file path.

Path layout (7 levels above the file name):
    year/month/event/subevent/category/supplemental category/rating

This is a CLIENT-SIDE concept only (see docs/design-metadata.md). It never reaches Immich:
before upload the place is merged into the image's metadata via
Metadata.patch_from_place, so the server only ever sees "pixels + metadata".

Levels 1-4 (year/month/event/subevent) form an image's album identity; levels 5-7
(category/supplemental/rating) are per-photo metadata (see docs/design-algorithm.md).
"""

from pathlib import Path

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

MONTH_NAMES = {v: k for k, v in MONTHS.items()}


class Place:
    def __init__(self,
                 year: int | None = None,
                 month: int | None = None,
                 event: str | None = None,
                 subevent: str | None = None,
                 category: str | None = None,
                 supplemental_category: str | None = None,
                 rating: int | None = None):
        self.year = year
        self.month = month
        self.event = event
        self.subevent = subevent
        self.category = category
        self.supplemental_category = supplemental_category
        self.rating = rating

    @classmethod
    def from_path_string(cls, file_path: Path | str) -> "Place":
        parts = Path(file_path).parts[:-1]
        if len(parts) != 7:
            raise ValueError(f"expected 7 directory levels, got {len(parts)}: {file_path}")
        year_s, month_s, event, subevent, category, supplemental_category, rating_s = parts
        return cls(
            year=int(year_s),
            month=MONTHS[month_s.lower()],
            event=event,
            subevent=subevent,
            category=category,
            supplemental_category=supplemental_category,
            rating=int(rating_s.split()[0]),
        )

    @classmethod
    def from_metadata(cls, metadata: "Metadata") -> "Place":
        date_time = metadata.date_time_original
        return cls(
            year=date_time.year if date_time else None,
            month=date_time.month if date_time else None,
            event=metadata.event,
            subevent=metadata.subevent,
            category=metadata.category,
            supplemental_category=metadata.supplemental_category,
            rating=metadata.rating,
        )
