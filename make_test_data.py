"""Generate the lightweight test photos under ``tests/data``.

Takes the (large) originals from the photo library, shrinks each to about
0.5 megapixel, re-encodes as JPEG at quality 20, and re-attaches *all* of the
original's metadata (EXIF incl. GPS, XMP, IPTC, ICC, comment, thumbnail) so the
result is a faithful, tiny stand-in. The originals are only ever read, never
written.

Usage:
    python make_test_data.py
"""

import math
from pathlib import Path

import pyexiv2
from PIL import Image

SRC_ROOT = Path("/Users/Shared/photolab").resolve()
DST_ROOT = Path(__file__).resolve().parent / "tests" / "data"

TARGET_PIXELS = 500_000
JPEG_QUALITY = 20
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp", ".heic", ".heif"}

# The three albums to snapshot, relative to SRC_ROOT. The first spans every
# rating folder under ``Impersonal``; the other two are single rating folders.
SOURCE_PREFIXES = [
    "2010/June/Misc/В поисках ЗАГСа/General/Impersonal",
    "2021/November/Misc/Свидание на Хануку/General/Impersonal/3 stars",
    "2022/July/Отпуск в Греции/Первая прогулка на Акрополь/General/Impersonal/3 stars",
]

# The dataset is committed with ASCII folder names; these event/subevent folders
# are written to tests/data under their English names (the originals stay as-is).
FOLDER_TRANSLATIONS = {
    "В поисках ЗАГСа": "In Search of the Registry Office",
    "Свидание на Хануку": "Hanukkah Date",
    "Отпуск в Греции": "Vacation in Greece",
    "Первая прогулка на Акрополь": "First Walk to the Acropolis",
}


def _target_size(width: int, height: int) -> tuple[int, int]:
    """Aspect-preserving size at or under TARGET_PIXELS (only ever shrinks)."""
    if width * height <= TARGET_PIXELS:
        return width, height
    scale = math.sqrt(TARGET_PIXELS / (width * height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _english(rel: Path) -> Path:
    """Rewrite any translated directory component to its English name."""
    parts = list(rel.parts)
    dirs = [FOLDER_TRANSLATIONS.get(p, p) for p in parts[:-1]]
    return Path(*dirs, parts[-1])


def reduce_image(src: Path, dst: Path) -> None:
    """Shrink ``src`` to ~0.5 MP (JPEG q20) and copy its metadata onto ``dst``."""
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    if not src.is_relative_to(SRC_ROOT):
        raise ValueError(f"refusing to read outside source root: {src}")
    if not dst.is_relative_to(DST_ROOT):
        raise ValueError(f"refusing to write outside data root: {dst}")

    dst.parent.mkdir(parents=True, exist_ok=True)

    # Re-encode the pixels: resize, then save a *clean* JPEG (drop the source's
    # embedded EXIF/ICC so PIL does not double-write segments that Exiv2 will
    # attach authoritatively below).
    with Image.open(src) as im:
        im.load()
        width, height = im.size
        new_w, new_h = _target_size(width, height)
        out = im if (new_w, new_h) == (width, height) else im.resize((new_w, new_h), Image.Resampling.LANCZOS)
        out.info = {}
        out.save(dst, "JPEG", quality=JPEG_QUALITY)

    # Re-attach every metadata segment; the file-backed context manager
    # persists the copied metadata on exit.
    with pyexiv2.Image(str(src)) as source, pyexiv2.Image(str(dst)) as dest:
        source.copy_to_another_image(dest)


def collect_sources() -> list[tuple[Path, Path]]:
    """All (source, destination) image pairs across SOURCE_PREFIXES."""
    pairs: list[tuple[Path, Path]] = []
    for prefix in SOURCE_PREFIXES:
        base = SRC_ROOT / prefix
        if not base.is_dir():
            raise FileNotFoundError(f"source album not found: {base}")
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
                continue
            pairs.append((path, DST_ROOT / _english(path.relative_to(SRC_ROOT))))
    return pairs


def main() -> None:
    pairs = collect_sources()
    print(f"Found {len(pairs)} images across {len(SOURCE_PREFIXES)} albums.\n")
    total_in = total_out = 0
    for src, dst in pairs:
        src_size = src.stat().st_size
        reduce_image(src, dst)
        dst_size = dst.stat().st_size
        total_in += src_size
        total_out += dst_size
        with Image.open(src) as s, Image.open(dst) as d:
            sw, sh = s.size
            dw, dh = d.size
        print(
            f"  {dst.relative_to(DST_ROOT).as_posix():70s} "
            f"{sw}x{sh} ({sw * sh / 1e6:.1f}MP) -> {dw}x{dh} ({dw * dh / 1e6:.1f}MP)  "
            f"{src_size / 1024:6.0f}KB -> {dst_size / 1024:6.0f}KB"
        )
    print(
        f"\nDone: {len(pairs)} files. "
        f"{total_in / 1024 / 1024:.1f}MB originals -> {total_out / 1024 / 1024:.1f}MB in tests/data"
    )


if __name__ == "__main__":
    main()
