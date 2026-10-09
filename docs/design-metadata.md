# Metadata & Upload Preparation — Low-Level Spec

## Scope
This document specifies, for the **SyncClient** side:

- the **7-level path layout** and the `Place` value object that parses it,
- the `Metadata` model and how it is read/patched/written,
- the **per-image upload preparation pipeline** that produces the "final file" which is
  hashed and uploaded,
- how the **content hash** (image identity) is computed,
- the set of **supported formats**.

It is the behavioural spec behind the existing `place.py` (`Place`) and `metadata.py`
(`Metadata`) modules, which are to be integrated into the final SyncClient.

## The 7-level layout (client-side only)
A local image lives at a path that is exactly **7 directory levels** above the file name:

```
year / month / event / subevent / category / supplemental_category / rating / FILENAME
```

| # | Level                 | Type / rule                                                        |
|---|-----------------------|--------------------------------------------------------------------|
| 1 | `year`                | 4-digit year (int)                                                 |
| 2 | `month`               | month name (`january`..`december`), case-insensitive → 1..12       |
| 3 | `event`               | free text                                                          |
| 4 | `subevent`            | free text                                                          |
| 5 | `category`            | free text                                                          |
| 6 | `supplemental_category`| free text                                                         |
| 7 | `rating`              | int (first whitespace-delimited token of the segment)              |

- The `Place` value object parses a relative path into these 7 fields. The path **must** be
  exactly 7 levels deep or parsing fails.
- The layout is a **client concept only**. It never reaches Immich: before upload the place
  is merged into the image's metadata so the server only ever sees "pixels + metadata",
  never the original file path (see `design.md` and `design-algorithm.md`).
- Levels 1–4 (`year/month/event/subevent`) form an image's **album identity**; levels
  5–7 (`category/supplemental/rating`) are per-photo metadata. This distinction is central
  to the sync algorithm.

## Metadata model
`Metadata` holds the fields read from / written to an image (tags mimic the PhotoLab
reference file):

| Field                   | Tag(s)                                                              |
|-------------------------|---------------------------------------------------------------------|
| `date_time_original`    | `Exif.Photo.DateTimeOriginal`                                       |
| `event`, `subevent`     | `Xmp.iptcExt.Event`, joined by an em-dash (` — `)                   |
| `category`              | `Iptc.Application2.Category` + `Xmp.photoshop.Category`             |
| `supplemental_category` | `Iptc.Application2.SuppCategory` + `Xmp.photoshop.SupplementalCategories` |
| `rating`                | `Xmp.xmp.Rating` (XMP spec Part 1: `-1` or `0..5`)                  |

- `Metadata.from_file(path)` reads all available fields from the file.
- **Client-side files are expected to be incomplete**: `event/subevent`, `category`,
  `supplemental_category`, and `rating` are often missing because their authoritative
  values live in the directory layout, not in the file.

## Upload preparation pipeline (per image)
For **every** local image the SyncClient performs, in order:

1. `place = Place.from_path_string(rel_path)` — parse the 7 levels from the file's relative
   path.
2. `md = Metadata.from_file(path)` — read whatever metadata the file already carries.
3. `md.patch_from_place(place)` — fill the **missing** fields (`event`, `subevent`,
   `category`, `supplemental_category`, `rating`) from the place. This does **not** touch
   `date_time_original`.
4. **Date/time rule.** The path carries only `year` and `month`. The original day and time
   **must** be preserved from the file's own `date_time_original` (read in step 2). We never
   fabricate a day/time from the path when the file already has one. Only when the file has
   **no** original date-time do we materialise a fallback of `year/month/01 00:00:00`.
   - `TODO`: pin down and test `write_to_file`'s date-time behaviour. The current
     implementation writes the date-time only when `write_date_time=True` and would fail if
     `date_time_original` is `None`; the intended "ensure the date-time comes from the
     original file, else fall back to year/month" rule needs to be implemented and covered
     by tests.
5. `md.write_to_file(...)` — write the augmented metadata back. The SyncClient keeps the
   resulting **final file in memory**.

The final file has:
- **pixels** identical to the original (metadata patching never alters pixel data);
- **metadata** complete enough for Immich to index and display (rating, category, event, …).

That final file is the **upload payload**. Its **pixel bit-stream** (encoded pixels,
metadata stripped) is the **hash source** (below).

## Content hashing (image identity)
- An image's identity is the **SHA512 of its pixel bit-stream**: the encoded pixel
  data with all metadata segments, chunks, or tags stripped. No decoding, no
  normalisation. Two files with identical pixels and encoding hash to the **same**
  value regardless of their metadata.
- **JPEG:** all APPn (0xE0–0xEF) and COM (0xFE) segments are stripped; the
  remaining SOI, DQT, DRI, SOFn, DHT, SOS, scan data, and EOI form the hash source.
- **PNG:** all ancillary chunks are stripped; the Signature, IHDR, PLTE, tRNS,
  IDAT, and IEND chunks form the hash source.
- **TIFF:** the strip/tile image data (referenced by StripOffsets/TileOffsets in
  the first IFD) is the hash source; all IFD tags (metadata) are excluded.
- A change to the **pixels** (or the **encoding**, e.g. JPEG↔PNG) changes the
  hash; a change to **metadata alone** does **not**.
- This hash is the **join key** between the local set and the remote set in the
  sync algorithm (`design-algorithm.md`).
- **Consequence:** a pixel change (re-photograph, edit, re-encode) yields a
  `DELETE`(old hash) + `UPLOAD`(new hash). A **metadata-only** change (re-rating,
  re-filing, a day/time change) keeps the same hash → a `MOVE` (the server
  updates the asset's stored metadata in Immich; no file re-upload). See
  `design-algorithm.md` §4.
- The **server** computes the same hash on collect by downloading the full asset
  bytes via `GET /assets/{id}` and stripping metadata the same way — no decoding
  (see `design-api.md`).

### Invariants (client and server must both hold)
- **Stored-bytes immutability:** `client_hash == server_hash` holds only if the
  bytes Immich stores and returns via `GET /assets/{id}` carry the **same pixel
  bit-stream** as the client's final file. Since the hash strips metadata, a
  metadata-only difference between the stored and local file does **not** break
  the hash — only a pixel or encoding difference does. This holds only if Immich
  stores the uploaded **original** without re-encoding and we always fetch the
  **original** (not a preview/thumbnail). **Verify against the target Immich
  version before relying on it.**
- **Deterministic (idempotent) metadata write:** the upload-preparation write
  must be idempotent — preparing the same file twice yields **byte-identical**
  output. The metadata write is lossless w.r.t. pixels, so the pixel bit-stream
  of the prepared file equals that of the original, and the hash is stable across
  runs.

## Supported formats
- **Supported:** jpeg, tiff, png.
- **Not supported:** RAW and any other format, unless the metadata write path is cheap and
  already supported by the current tooling. (Content hashing strips metadata from the
  encoded bit-stream — no decoding — so format support is gated only by metadata
  write-back.)
- `TODO`: fix the accepted-extension list in the client (the test file currently lists many
  extensions, e.g. RAW, which are out of scope) to exactly the supported set.

## Open items (tracked)
- `TODO` `write_to_file` date-time guarantee (step 4 above).
- `TODO` narrow the accepted extension list to the supported formats.
