"""Record — the unit of comparison in the sync algorithm.

See docs/design-algorithm.md §1.2: a record carries a photo's managed metadata,
at minimum:

    | field                   | source (local / remote)                       |
    |-------------------------|-----------------------------------------------|
    | year, month             | place (levels 1-2) / date_time_original       |
    | event, subevent         | place (levels 3-4) / Xmp.iptcExt.Event        |
    | category                | place (level 5) / IPTC+XMP                    |
    | supplemental_category   | place (level 6) / IPTC+XMP                    |
    | rating                  | place (level 7) / Xmp.xmp.Rating              |
    | date_time_original      | full Exif.Photo.DateTimeOriginal (day + time) |

- Album identity = (year, month, event, subevent) — the first 4 fields.
- Position = all 7 place fields (levels 1..7).
- Two records differ in ANY field (including date_time_original) => the photo
  must be MOVED (design-algorithm.md §4).
- The 7-level path is a *projection* of the record (it drops the day/time of
  date_time_original). The file name is not part of the record and never is.

On the local side every field is filled (the 7-level layout provides 1-7 and
the date-time rule in design-metadata.md fills date_time_original), so local
records are complete. Remote records reconstructed from embedded metadata may
have missing fields (None) — such a photo is only ever MOVED into a valid
position, never created (design-algorithm.md §8).
"""

from dataclasses import dataclass
from datetime import datetime

from place import MONTH_NAMES

EM_DASH = " \u2014 "


@dataclass(frozen=True)
class Record:
    year: int | None = None
    month: int | None = None
    event: str | None = None
    subevent: str | None = None
    category: str | None = None
    supplemental_category: str | None = None
    rating: int | None = None
    date_time_original: datetime | None = None

    @property
    def album_identity(self) -> tuple | None:
        """The 4-level identity (year, month, event, subevent), or None if any part is missing."""
        if None in (self.year, self.month, self.event, self.subevent):
            return None
        return (self.year, self.month, self.event, self.subevent)

    @property
    def album_name(self) -> str | None:
        """The Immich album display name: 'event — subevent'."""
        if self.event is None or self.subevent is None:
            return None
        return f"{self.event}{EM_DASH}{self.subevent}"

    @property
    def position(self) -> tuple:
        """The 7-level position (levels 1..7), the path projection of the record."""
        return (
            self.year, self.month, self.event, self.subevent,
            self.category, self.supplemental_category, self.rating,
        )

    def to_path_string(self, file_name: str | None = None) -> str:
        """The 7-level path projection (canonical month name, no file name by default)."""
        month = MONTH_NAMES[self.month] if self.month else "?"
        parts = [
            str(self.year if self.year is not None else "?"),
            month,
            self.event or "?",
            self.subevent or "?",
            self.category or "?",
            self.supplemental_category or "?",
            str(self.rating if self.rating is not None else "?"),
        ]
        if file_name:
            parts.append(file_name)
        return "/".join(parts)

    def to_dict(self) -> dict:
        """The wire form used by the collect result and operation payloads (design-api.md §6.2/§6.3)."""
        return {
            "year": self.year,
            "month": self.month,
            "event": self.event,
            "subevent": self.subevent,
            "category": self.category,
            "supplemental_category": self.supplemental_category,
            "rating": self.rating,
            "date_time_original": self.date_time_original.isoformat()
            if self.date_time_original else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        dtp = d.get("date_time_original")
        return cls(
            year=d.get("year"),
            month=d.get("month"),
            event=d.get("event"),
            subevent=d.get("subevent"),
            category=d.get("category"),
            supplemental_category=d.get("supplemental_category"),
            rating=d.get("rating"),
            date_time_original=datetime.fromisoformat(dtp) if dtp else None,
        )

    def __str__(self) -> str:
        dtp = self.date_time_original.isoformat(sep=" ") if self.date_time_original else "?"
        return f"{self.to_path_string()} [{dtp}]"


def format_identity(identity: tuple) -> str:
    """'year/month/event/subevent' — the album addressing form used in the API (design-api.md §6.3)."""
    year, month, event, subevent = identity
    return f"{year}/{month}/{event}/{subevent}"


def parse_identity(s: str) -> tuple:
    year_s, month_s, event, subevent = s.split("/", 3)
    return (int(year_s), int(month_s), event, subevent)
