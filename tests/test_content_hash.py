"""Tests for content_hash.compute_content_hash (docs/design-metadata.md).

The hash is SHA512 over the **pixel bit-stream**: the encoded pixel data with
all metadata segments/chunks/tags stripped. No decoding, no normalisation. Two
files with identical pixels (and encoding) hash to the same value even if
their metadata differs. A pixel or encoding change changes the hash; a
metadata-only change does not.
"""

import hashlib
import io
from pathlib import Path

import pyexiv2
from PIL import Image

from content_hash import compute_content_hash

DATA_DIR = Path(__file__).parent / "data"


def _bytes(image: Image.Image, fmt: str, **save_kwargs) -> bytes:
    buf = io.BytesIO()
    image.save(buf, fmt, **save_kwargs)
    return buf.getvalue()


def _write(data: bytes, name: str) -> Path:
    path = DATA_DIR.parent / name
    path.write_bytes(data)
    return path


def test_pixel_change_changes_the_hash():
    a = _bytes(Image.new("RGB", (8, 8), (0, 0, 0)), "JPEG")
    b = _bytes(Image.new("RGB", (8, 8), (255, 255, 255)), "JPEG")
    assert compute_content_hash(a) != compute_content_hash(b)


def test_same_pixels_different_formats_different_hash():
    # same visual pixels in different containers -> different pixel bit-stream
    pixels = Image.new("RGB", (8, 8), (200, 30, 30))
    assert compute_content_hash(_bytes(pixels, "PNG")) != compute_content_hash(_bytes(pixels, "TIFF"))


def test_metadata_change_keeps_the_hash():
    # a metadata-only change does NOT alter the pixel bit-stream -> same hash
    pixels = Image.new("RGB", (8, 8), (10, 200, 30))
    plain = _bytes(pixels, "JPEG")
    tagged = _write(plain, "_probe_content_tagged.jpg")
    try:
        with pyexiv2.Image(str(tagged)) as img:
            img.modify_xmp({"Xmp.xmp.Rating": "5"})
            img.modify_exif({"Exif.Photo.DateTimeOriginal": "2026:04:05 10:20:30"})
        assert compute_content_hash(tagged.read_bytes()) == compute_content_hash(plain)
    finally:
        tagged.unlink(missing_ok=True)


def test_alpha_changes_the_hash():
    # an RGBA PNG and an RGB PNG are different pixel data even for the same colour
    rgb = Image.new("RGB", (8, 8), (200, 30, 30))
    rgba = Image.new("RGBA", (8, 8), (200, 30, 30, 128))
    assert compute_content_hash(_bytes(rgb, "PNG")) != compute_content_hash(_bytes(rgba, "PNG"))


def test_unsupported_format_falls_back_to_raw_bytes():
    data = b"not-an-image: fallback to raw bytes"
    assert compute_content_hash(data) == hashlib.sha512(data).hexdigest()


# Pinned digests of the two reference photos (pixel bit-stream): an accidental
# change of the on-disk originals is caught here. The rest of tests/data is a
# reduced dataset exercised by test_metadata.py, not pinned here.
EXPECTED_DIGESTS = {
    "IMG_8677_1.jpeg":
        "d80c1dd88758014e7460daa60f39949d55b0eedbafcc6dffffff8c032d44ec783d5130c4967d9963bcd7743f5047efbc628e4d930b7595e46e0d5c7d16686243",
    "IMG_8773_1.jpg":
        "b6caec90fa17ed3e3b69138803259fcea41f89969bd5b5d6fbe9f20a55c8e43a8055c007a049ef4c60589c97e04c4cd0a25ee6c689402a333d005133c9a13179",
}


def test_reference_file_digests_are_stable():
    by_name = {p.name: p for p in DATA_DIR.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg"}}
    for name, expected in EXPECTED_DIGESTS.items():
        assert name in by_name, f"reference photo {name} is missing from tests/data"
        assert compute_content_hash(by_name[name].read_bytes()) == expected
