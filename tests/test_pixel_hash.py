"""Tests for pixel_hash.compute_pixel_hash (docs/design-metadata.md).

The hash is SHA512 over the decoded 8-bit RGB pixels only: metadata and
container bytes are excluded, alpha is dropped, 16-bit depth is normalised to
8-bit, and EXIF orientation is NOT applied (the stored buffer is hashed).
"""

import io
from pathlib import Path

import pyexiv2
from PIL import Image

from pixel_hash import compute_pixel_hash

DATA_DIR = Path(__file__).parent / "pyexiv2" / "data"


def _bytes(image: Image.Image, fmt: str, **save_kwargs) -> bytes:
    buf = io.BytesIO()
    image.save(buf, fmt, **save_kwargs)
    buf.seek(0)
    return buf.read()


def _write(data: bytes, name: str) -> Path:
    path = DATA_DIR.parent / name
    path.write_bytes(data)
    return path


def test_same_pixels_different_lossless_formats_same_hash():
    # identical decoded pixels in different lossless containers -> same hash
    pixels = Image.new("RGB", (8, 8), (200, 30, 30))
    assert compute_pixel_hash(_bytes(pixels, "PNG")) == compute_pixel_hash(_bytes(pixels, "TIFF"))


def test_hash_is_over_decoded_pixels_not_container():
    # JPEG is lossy: the hash is over the *decoded* pixels, so a JPEG and a
    # lossless container of those same decoded pixels hash identically
    pixels = Image.new("RGB", (8, 8), (200, 30, 30))
    jpg = _bytes(pixels, "JPEG")
    decoded = Image.open(io.BytesIO(jpg)).convert("RGB")
    assert compute_pixel_hash(jpg) == compute_pixel_hash(_bytes(decoded, "PNG"))


def test_metadata_does_not_change_the_hash():
    # the join key: a metadata-only change keeps the same hash (design-metadata.md)
    pixels = Image.new("RGB", (8, 8), (10, 200, 30))
    plain = _bytes(pixels, "JPEG")
    tagged = _write(plain, "_probe_hash_tagged.jpg")
    try:
        with pyexiv2.Image(str(tagged)) as img:
            img.modify_xmp({"Xmp.xmp.Rating": "5"})
            img.modify_exif({"Exif.Photo.DateTimeOriginal": "2026:04:05 10:20:30"})
        assert compute_pixel_hash(tagged.read_bytes()) == compute_pixel_hash(plain)
    finally:
        tagged.unlink(missing_ok=True)


def test_alpha_is_dropped():
    rgb = Image.new("RGB", (8, 8), (200, 30, 30))
    rgba = Image.new("RGBA", (8, 8), (200, 30, 30, 128))
    assert compute_pixel_hash(_bytes(rgb, "PNG")) == compute_pixel_hash(_bytes(rgba, "PNG"))


def test_16bit_is_normalised_to_8bit():
    # v in 8-bit == v*257 in 16-bit: the deep TIFF hashes like its 8-bit twin
    grad = [0, 128, 255] * 5 + [0]
    shallow = Image.new("L", (4, 4)); shallow.putdata(grad)
    deep = Image.new("I;16", (4, 4)); deep.putdata([v * 257 for v in grad])
    assert compute_pixel_hash(_bytes(shallow, "TIFF")) == compute_pixel_hash(_bytes(deep, "TIFF"))


def test_orientation_tag_does_not_apply():
    # the stored buffer is hashed as decoded: adding the orientation tag does
    # not change the hash, physically rotating the buffer does. A non-uniform
    # image is used so the rotation actually rearranges the pixels.
    pixels = Image.new("RGB", (4, 2))
    pixels.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)])
    base = _bytes(pixels, "JPEG")
    base_hash = compute_pixel_hash(base)
    tagged = _write(base, "_probe_hash_orientation.jpg")
    try:
        with pyexiv2.Image(str(tagged)) as img:
            img.modify_exif({"Exif.Image.Orientation": 6})
        assert compute_pixel_hash(tagged.read_bytes()) == base_hash
    finally:
        tagged.unlink(missing_ok=True)
    rotated = _bytes(pixels.transpose(Image.Transpose.ROTATE_90), "JPEG")
    assert compute_pixel_hash(rotated) != base_hash


def test_different_pixels_different_hash():
    a = _bytes(Image.new("RGB", (8, 8), (0, 0, 0)), "JPEG")
    b = _bytes(Image.new("RGB", (8, 8), (255, 255, 255)), "JPEG")
    assert compute_pixel_hash(a) != compute_pixel_hash(b)


# Pinned digests of the two reference photos: an accidental change of the
# canonical pixel form (e.g. a different normalisation) is caught here.
EXPECTED_DIGESTS = {
    "IMG_8677_1.jpeg":
        "d0e183bcd8a94e3edf63f30643a942fb312c0090a79cea4dd29ff76a053dcb853c"
        "6b927e268aba78e85bbdd193d4f60a374e105a2bbbb578f4adbc99040f3780",
    "IMG_8773_1.jpg":
        "be0c45d35c2217787e0434558a1c614c7d5e400b007f5aa7d53a5cdf696aaf02e2"
        "fa36411dbedc3fd3990a9313d814f03d3756bc998d1f59c045a8a69398672e",
}


def test_reference_file_digests_are_stable():
    for path in sorted(DATA_DIR.rglob("*")):
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            assert compute_pixel_hash(path.read_bytes()) == EXPECTED_DIGESTS[path.name]
