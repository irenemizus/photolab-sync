"""Tests for content_hash.compute_content_hash (docs/design-metadata.md).

The hash is SHA512 over the **pixel bit-stream**: the encoded pixel data with
all metadata segments/chunks/tags stripped. No decoding, no normalisation. Two
files with identical pixels (and encoding) hash to the same value even if
their metadata differs. A pixel or encoding change changes the hash; a
metadata-only change does not.
"""

import hashlib
import io
import struct
import zlib
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


# --- JPEG per-format behaviour ----------------------------------------------

def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def _jpeg_with_segments(data: bytes, *segments: bytes) -> bytes:
    return data[:2] + b"".join(segments) + data[2:]


def test_jpeg_appn_segments_are_stripped():
    base = _bytes(Image.new("RGB", (16, 16), (90, 20, 200)), "JPEG")
    tagged = _jpeg_with_segments(
        base,
        _jpeg_segment(0xE1, b"Exif\x00\x00" + b"\x00" * 32),              # APP1
        _jpeg_segment(0xE2, b"ICC_PROFILE\x00\x01\x00" + b"\x00" * 48),   # APP2
        _jpeg_segment(0xEC, b"Photoshop placeholder"),                    # APP12
    )
    assert compute_content_hash(tagged) == compute_content_hash(base)


def test_jpeg_com_segment_is_stripped():
    base = _bytes(Image.new("RGB", (16, 16), (200, 20, 90)), "JPEG")
    tagged = _jpeg_with_segments(base, _jpeg_segment(0xFE, b"comment metadata"))
    assert compute_content_hash(tagged) == compute_content_hash(base)


def test_jpeg_progressive_is_a_different_encoding():
    img = Image.new("RGB", (16, 16), (10, 100, 200))
    assert compute_content_hash(_bytes(img, "JPEG", progressive=True)) != \
        compute_content_hash(_bytes(img, "JPEG"))


def test_jpeg_bytes_after_eoi_are_ignored():
    base = _bytes(Image.new("RGB", (16, 16), (5, 5, 5)), "JPEG")
    assert compute_content_hash(base + b"trailing junk") == compute_content_hash(base)


# --- PNG per-format behaviour -------------------------------------------------

def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + ctype + payload + \
        struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)


def _png_with_chunks(data: bytes, *chunks: bytes) -> bytes:
    return data[:8] + b"".join(chunks) + data[8:]


def test_png_ancillary_chunks_are_stripped():
    base = _bytes(Image.new("RGB", (16, 16), (200, 90, 20)), "PNG")
    tagged = _png_with_chunks(
        base,
        _png_chunk(b"tEXt", b"Comment\x00a note"),
        _png_chunk(b"iCCP", b"profile\x00" + b"\x00" * 64),
        _png_chunk(b"gAMA", struct.pack(">I", 45455)),
    )
    assert compute_content_hash(tagged) == compute_content_hash(base)


def test_png_exif_chunk_is_stripped():
    base = _bytes(Image.new("RGB", (16, 16), (30, 200, 120)), "PNG")
    tagged = _png_with_chunks(base, _png_chunk(b"eXIf", b"Exif\x00\x00" + b"\x00" * 24))
    assert compute_content_hash(tagged) == compute_content_hash(base)


def test_png_ihdr_change_changes_the_hash():
    small = _bytes(Image.new("RGB", (8, 8), (1, 2, 3)), "PNG")
    big = _bytes(Image.new("RGB", (16, 16), (1, 2, 3)), "PNG")
    assert compute_content_hash(small) != compute_content_hash(big)


# --- TIFF per-format behaviour ------------------------------------------------

def _make_tiff(pixels: bytes = b"", endian: str = "<", extra_tags=(),
               strips: list[bytes] | None = None, tiled: bool = False) -> bytes:
    """Minimal single-IFD 8x8x3 uncompressed RGB TIFF holding `strips` of strip/tile data."""
    e = endian
    if strips is None:
        strips = [pixels]
    off_tag, cnt_tag = (324, 325) if tiled else (273, 279)
    header = b"II\x2a\x00" if e == "<" else b"MM\x00\x2a"
    pad = (lambda v: v.ljust(4, b"\x00")) if e == "<" else (lambda v: v.rjust(4, b"\x00"))
    std_tags = [
        (256, 4, 1, struct.pack(e + "I", 8)),     # ImageWidth
        (257, 4, 1, struct.pack(e + "I", 8)),     # ImageLength
        (258, 3, 3, struct.pack(e + "3H", 8, 8, 8)),  # BitsPerSample
        (259, 3, 1, struct.pack(e + "H", 1)),     # Compression
        (262, 3, 1, struct.pack(e + "H", 2)),     # PhotometricInterpretation
        (277, 3, 1, struct.pack(e + "H", 3)),     # SamplesPerPixel
        (278, 4, 1, struct.pack(e + "I", 8)),     # RowsPerStrip (not a byte count)
    ]
    n_entries = len(std_tags) + len(extra_tags) + 2
    ifd_off = 8
    pos = ifd_off + 2 + n_entries * 12 + 4

    entries = []
    blobs = b""
    for tag, typ, count, value in (*std_tags, *extra_tags):
        if len(value) <= 4:
            inline = pad(value)
        else:
            inline = struct.pack(e + "I", pos)
            blobs += value
            pos += len(value)
        entries.append(struct.pack(e + "HHI", tag, typ, count) + inline)

    offsets = []
    counts = []
    p = pos + 8 * len(strips)  # room for the two arrays (when they need pointers)
    for s in strips:
        offsets.append(p)
        counts.append(len(s))
        p += len(s)
    if len(strips) == 1:  # one LONG fits in the value field: stored inline
        off_val = struct.pack(e + "I", offsets[0])
        cnt_val = struct.pack(e + "I", counts[0])
    else:
        off_val = struct.pack(e + "I", pos)
        cnt_val = struct.pack(e + "I", pos + 4 * len(strips))
    off_arr = b"".join(struct.pack(e + "I", v) for v in offsets)
    cnt_arr = b"".join(struct.pack(e + "I", v) for v in counts)
    entries.append(struct.pack(e + "HHI", off_tag, 4, len(strips)) + off_val)
    entries.append(struct.pack(e + "HHI", cnt_tag, 4, len(strips)) + cnt_val)

    ifd = struct.pack(e + "H", n_entries) + b"".join(entries) + struct.pack(e + "I", 0)
    return header + struct.pack(e + "I", ifd_off) + ifd + blobs + off_arr + cnt_arr + b"".join(strips)


def test_tiff_hashes_only_the_strip_data():
    pixels = bytes(range(192))
    data = _make_tiff(pixels)
    assert compute_content_hash(data) == hashlib.sha512(pixels).hexdigest()
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (8, 8)
        assert img.get_flattened_data() == tuple(tuple(pixels[i:i + 3]) for i in range(0, 192, 3))


def test_tiff_metadata_tags_are_stripped():
    pixels = bytes(range(192))
    plain = _make_tiff(pixels)
    tagged = _make_tiff(
        pixels,
        extra_tags=[
            (270, 7, 24, b"ImageDescription\x00" + b"\x00" * 11),  # ASCII, external pointer
            (271, 7, 6, b"Make\x00\x00"),                          # ASCII, external pointer
        ],
    )
    assert compute_content_hash(tagged) == compute_content_hash(plain)


def test_tiff_rows_per_strip_is_not_the_byte_count():
    # Regression: StripByteCounts is tag 279, not 278 (RowsPerStrip); with the
    # wrong tag only the first `RowsPerStrip` bytes of the strip were hashed.
    pixels = bytes(range(192))
    data = _make_tiff(pixels)  # the builder always carries a RowsPerStrip=8 entry
    assert compute_content_hash(data) == hashlib.sha512(pixels).hexdigest()


def test_tiff_big_and_little_endian_hash_the_same_pixels():
    pixels = bytes(range(192))
    le = _make_tiff(pixels, endian="<")
    be = _make_tiff(pixels, endian=">")
    assert compute_content_hash(le) == hashlib.sha512(pixels).hexdigest()
    assert compute_content_hash(be) == hashlib.sha512(pixels).hexdigest()
    assert compute_content_hash(le) == compute_content_hash(be)


def test_tiff_multi_strip_concatenates_in_order():
    a = bytes(range(96))
    b = bytes(range(96, 192))
    assert compute_content_hash(_make_tiff(strips=[a, b])) == \
        compute_content_hash(_make_tiff(a + b)) == \
        hashlib.sha512(a + b).hexdigest()


def test_tiff_tiles_use_the_same_pixel_bytes():
    a = bytes(range(96))
    b = bytes(range(96, 192))
    assert compute_content_hash(_make_tiff(strips=[a, b], tiled=True)) == \
        compute_content_hash(_make_tiff(strips=[a, b]))


def test_tiff_pixel_change_changes_the_hash():
    a = _make_tiff(bytes(192))
    b = _make_tiff(bytes(range(192)))
    assert compute_content_hash(a) != compute_content_hash(b)


def test_tiff_without_strip_tags_falls_back_to_raw_bytes():
    data = b"II\x2a\x00" + struct.pack("<I", 8) + struct.pack("<H", 0) + struct.pack("<I", 0)
    assert compute_content_hash(data) == hashlib.sha512(data).hexdigest()


# --- Real photos: convert, re-tag, re-hash -----------------------------------

def _jpeg_paths() -> list[Path]:
    paths = sorted(p for p in DATA_DIR.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg"})
    assert len(paths) >= 5, f"need 5 JPEGs in {DATA_DIR}, found {len(paths)}"
    return paths[:5]


def _re_tag(path: Path) -> None:
    with pyexiv2.Image(str(path)) as img:
        img.modify_xmp({"Xmp.xmp.Rating": "5"})
        img.modify_exif({"Exif.Photo.DateTimeOriginal": "2026:04:05 10:20:30"})


def test_jpeg_to_png_metadata_roundtrip(tmp_path):
    for src in _jpeg_paths():
        with Image.open(src) as img:
            png = _bytes(img.convert("RGB"), "PNG")
        assert compute_content_hash(png) != compute_content_hash(src.read_bytes())
        dst = tmp_path / f"{src.stem}.png"
        dst.write_bytes(png)
        _re_tag(dst)
        assert compute_content_hash(dst.read_bytes()) == compute_content_hash(png)


def test_jpeg_to_tiff_metadata_roundtrip(tmp_path):
    for src in _jpeg_paths():
        with Image.open(src) as img:
            tiff = _bytes(img.convert("RGB"), "TIFF")
        assert compute_content_hash(tiff) != compute_content_hash(src.read_bytes())
        dst = tmp_path / f"{src.stem}.tif"
        dst.write_bytes(tiff)
        _re_tag(dst)
        assert compute_content_hash(dst.read_bytes()) == compute_content_hash(tiff)


def test_jpeg_retag_keeps_the_hash(tmp_path):
    for src in _jpeg_paths():
        dst = tmp_path / f"{src.stem}_tagged.jpg"
        dst.write_bytes(src.read_bytes())
        _re_tag(dst)
        assert compute_content_hash(dst.read_bytes()) == compute_content_hash(src.read_bytes())
