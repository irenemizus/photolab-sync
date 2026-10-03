"""Tests for content_hash.compute_content_hash (docs/design-metadata.md).

The hash is SHA512 over the RAW FILE BYTES: pixels, container/encoding bytes,
and all metadata are part of the identity. No decoding, no normalisation. Two
files that differ in any byte hash to a different value.
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
    buf.seek(0)
    return buf.read()


def _write(data: bytes, name: str) -> Path:
    path = DATA_DIR.parent / name
    path.write_bytes(data)
    return path


def test_hash_is_sha512_of_raw_bytes():
    data = b"photolab-sync: content is the file, exactly as stored"
    assert compute_content_hash(data) == hashlib.sha512(data).hexdigest()


def test_different_bytes_different_hash():
    a = _bytes(Image.new("RGB", (8, 8), (0, 0, 0)), "JPEG")
    b = _bytes(Image.new("RGB", (8, 8), (255, 255, 255)), "JPEG")
    assert compute_content_hash(a) != compute_content_hash(b)


def test_same_pixels_different_formats_different_hash():
    # same visual pixels in different containers -> different BYTES -> different
    # hash (the inverse of the old pixel-hash behaviour)
    pixels = Image.new("RGB", (8, 8), (200, 30, 30))
    assert compute_content_hash(_bytes(pixels, "PNG")) != compute_content_hash(_bytes(pixels, "TIFF"))


def test_metadata_change_changes_the_hash():
    # a metadata-only change alters the file bytes -> a new hash (design-metadata.md)
    pixels = Image.new("RGB", (8, 8), (10, 200, 30))
    plain = _bytes(pixels, "JPEG")
    tagged = _write(plain, "_probe_content_tagged.jpg")
    try:
        with pyexiv2.Image(str(tagged)) as img:
            img.modify_xmp({"Xmp.xmp.Rating": "5"})
            img.modify_exif({"Exif.Photo.DateTimeOriginal": "2026:04:05 10:20:30"})
        assert compute_content_hash(tagged.read_bytes()) != compute_content_hash(plain)
    finally:
        tagged.unlink(missing_ok=True)


def test_alpha_changes_the_hash():
    # an RGBA PNG and an RGB PNG are different bytes even for the same colour
    rgb = Image.new("RGB", (8, 8), (200, 30, 30))
    rgba = Image.new("RGBA", (8, 8), (200, 30, 30, 128))
    assert compute_content_hash(_bytes(rgb, "PNG")) != compute_content_hash(_bytes(rgba, "PNG"))


# Pinned digests of the two reference photos (raw file bytes): an accidental
# change of the on-disk originals is caught here.
EXPECTED_DIGESTS = {
    "IMG_8677_1.jpeg":
        "00bc7e735a0958ad731ce21a7043a98e9ece57311a797b45c9dcdc331a43e229952a84d3a1e51951c55944cf953011c9de628e2d4425c1efe1ab8fcf95c26c25",
    "IMG_8773_1.jpg":
        "46e4312f6b3f00ba031f31a1784241b0822738dda517e255426d40cf4bd64f1354ffa9063aa37848356447f8a8c2046a6becd34d81f40bdd5aec1a9344839a6b",
}


def test_reference_file_digests_are_stable():
    for path in sorted(DATA_DIR.rglob("*")):
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            assert compute_content_hash(path.read_bytes()) == EXPECTED_DIGESTS[path.name]
