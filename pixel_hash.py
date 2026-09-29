"""Content hash: SHA512 over the decoded pixel data only.

See docs/design-metadata.md, "Content hashing (image identity)":

- The file is decoded (container bytes -> pixel buffer) and the digest is
  computed over the pixels. Container/encoding bytes and ALL metadata are
  excluded. Two files with identical pixels but different metadata or container
  encoding hash to the SAME value.
- This hash is the join key between the local and the remote set
  (docs/design-algorithm.md §1). Changing only metadata (rating, category,
  moving folders) does NOT change the hash — such a change is a MOVE, not a
  delete+upload.

Canonical pixel form (both the client and the server MUST decode identically,
docs/design-metadata.md "pixel-buffer normalisation"):

- decode to 8-bit RGB and drop alpha (a flat JPEG and a same-pixel
  PNG-with-transparency hash the same);
- 16-bit (deep TIFF) is normalised to the canonical 8-bit form;
- EXIF orientation is NOT applied — the stored buffer is hashed as decoded;
- no resampling / colour management — the decoder's raw output is hashed;
- multi-frame / preview: the primary (first) image only.
"""

import hashlib
import io
import struct

from PIL import Image

# Modes Pillow loads 16-bit (deep) TIFFs as.
_SIXTEEN_BIT_MODES = {"I;16", "I;16L", "I;16B"}


def compute_pixel_hash(data: bytes) -> str:
    """SHA512 hex digest of the canonical pixels (8-bit RGB) of the given image bytes."""
    image = Image.open(io.BytesIO(data))
    return hashlib.sha512(_canonical_pixels(image)).hexdigest()


def _canonical_pixels(image: Image.Image) -> bytes:
    """The canonical pixel buffer: 8-bit RGB (design-metadata.md).

    convert("RGB") loads the primary frame and drops alpha, and EXIF orientation
    is left untouched by the decoder — but it CLAMPS 16-bit samples to 8-bit
    instead of scaling them, so 16-bit depths are scaled by //257 first
    (v in 8-bit == v*257 in 16-bit).
    """
    if image.mode in _SIXTEEN_BIT_MODES:
        image = _scale_16_to_8(image)
    return image.convert("RGB").tobytes()


def _scale_16_to_8(image: Image.Image) -> Image.Image:
    """Scale a 16-bit image to 8-bit (v // 257), for any band count.

    Pillow has no direct 16→8 arithmetic, so the raw little-endian 16-bit
    samples are scaled explicitly and repacked as an 8-bit image.
    """
    bands = len(image.getbands())
    width, height = image.size
    raw = image.tobytes()
    samples = struct.unpack(f"<{len(raw) // 2}H", raw)
    scaled = bytes(min(255, v // 257) for v in samples)
    mode = {1: "L", 3: "RGB", 4: "RGBA"}[bands]
    return Image.frombytes(mode, (width, height), scaled)
