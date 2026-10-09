"""Content hash: SHA512 over the pixel bit-stream (encoded pixels, metadata stripped).

See docs/design-metadata.md, "Content hashing (image identity)":

- The digest is computed over the file's **encoded pixel data** with all
  metadata segments, chunks, or tags removed. No decoding, no normalisation.
  Two files with identical pixels and encoding hash to the **same** value even
  if their metadata differs.
- A change to the **pixels** (or the **encoding**, e.g. JPEG-to-PNG) changes
  the hash; a change to **metadata alone** does **not**.
- This hash is the join key between the local and the remote set
  (docs/design-algorithm.md §1). A pixel change is a DELETE(old) + UPLOAD(new);
  a metadata-only change is a MOVE (§4).
- The server computes the same hash on collect by downloading the full asset
  bytes and stripping metadata the same way (design-metadata.md invariants).

Format-specific extraction:
  JPEG: strip all APPn (0xE0-0xEF) and COM (0xFE) segments; keep SOI, DQT,
        DRI, SOFn, DHT, SOS, scan data, EOI.
  PNG:  keep Signature, IHDR, PLTE, tRNS, IDAT, IEND; strip all ancillary.
  TIFF: extract the strip/tile image data from the first IFD.
  Unknown: fall back to the raw bytes.
"""

import hashlib
import struct


def compute_content_hash(data: bytes) -> str:
    """SHA512 hex digest of the pixel bit-stream (metadata stripped)."""
    return hashlib.sha512(_pixel_bytes(data)).hexdigest()


def _pixel_bytes(data: bytes) -> bytes:
    if data[:2] == b"\xff\xd8":
        return _jpeg_pixel_bytes(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_pixel_bytes(data)
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return _tiff_pixel_bytes(data)
    return data


# --- JPEG -------------------------------------------------------------------

def _jpeg_pixel_bytes(data: bytes) -> bytes:
    n = len(data)
    if n < 4 or data[0:2] != b"\xff\xd8":
        return data
    out = bytearray(data[0:2])  # SOI
    pos = 2
    while pos + 2 <= n:
        if data[pos] != 0xFF:
            out += data[pos:]
            break
        marker = data[pos + 1]
        if marker == 0xD9:  # EOI
            out += data[pos:pos + 2]
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # TEM, RSTn
            out += data[pos:pos + 2]
            pos += 2
            continue
        if pos + 4 > n:
            break
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        seg_end = pos + 2 + length
        if seg_end > n:
            break
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:
            pos = seg_end  # APPn or COM: metadata, strip
        else:
            out += data[pos:seg_end]  # DQT, DRI, SOFn, DHT, SOS, DNL
            pos = seg_end
            if marker == 0xDA:  # SOS: rest is scan data until EOI
                eoi = data.find(b"\xff\xd9", pos)
                if eoi == -1:
                    out += data[pos:]
                else:
                    out += data[pos:eoi + 2]
                break
    return bytes(out)


# --- PNG --------------------------------------------------------------------

_PNG_KEEP = (b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND")


def _png_pixel_bytes(data: bytes) -> bytes:
    n = len(data)
    if n < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return data
    out = bytearray(data[:8])  # signature
    pos = 8
    while pos + 8 <= n:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_end = pos + 12 + length
        if chunk_end > n:
            break
        if chunk_type in _PNG_KEEP:
            out += data[pos:chunk_end]
        pos = chunk_end
    return bytes(out)


# --- TIFF -------------------------------------------------------------------

def _tiff_pixel_bytes(data: bytes) -> bytes:
    n = len(data)
    if n < 8:
        return data
    if data[0:2] == b"II":
        endian = "<"
    elif data[0:2] == b"MM":
        endian = ">"
    else:
        return data
    if struct.unpack(endian + "H", data[2:4])[0] != 42:
        return data
    first_ifd = struct.unpack(endian + "I", data[4:8])[0]
    if first_ifd + 2 > n:
        return data
    n_entries = struct.unpack(endian + "H", data[first_ifd:first_ifd + 2])[0]

    offsets: list[int] = []
    counts: list[int] = []
    for i in range(n_entries):
        off = first_ifd + 2 + i * 12
        if off + 12 > n:
            break
        tag = struct.unpack(endian + "H", data[off:off + 2])[0]
        if tag not in (273, 278, 324, 325):  # StripOffsets, StripByteCounts, TileOffsets, TileByteCounts
            continue
        typ = struct.unpack(endian + "H", data[off + 2:off + 4])[0]
        count = struct.unpack(endian + "I", data[off + 4:off + 8])[0]
        vals = _tiff_read_array(data, endian, typ, count, off + 8, n)
        if vals is None:
            continue
        if tag in (273, 324):
            offsets.extend(vals)
        else:
            counts.extend(vals)

    if not offsets or len(offsets) != len(counts):
        return data

    out = bytearray()
    for o, c in zip(offsets, counts):
        if 0 <= o and o + c <= n:
            out += data[o:o + c]
    return bytes(out)


def _tiff_read_array(data: bytes, endian: str, typ: int, count: int,
                     value_off: int, n: int) -> list[int] | None:
    if typ == 3:  # SHORT
        size = 2 * count
    elif typ == 4:  # LONG
        size = 4 * count
    else:
        return None
    if size <= 4:
        ptr = value_off
    else:
        if value_off + 4 > n:
            return None
        ptr = struct.unpack(endian + "I", data[value_off:value_off + 4])[0]
    if ptr + size > n:
        return None
    fmt_char = "H" if typ == 3 else "I"
    return list(struct.unpack(endian + f"{count}{fmt_char}", data[ptr:ptr + size]))
