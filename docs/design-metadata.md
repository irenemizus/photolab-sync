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

That final file is the **upload payload**, and its **raw bytes** are the **hash source**
(below).

## Content hashing (image identity)
- An image's identity is the **SHA512 of its raw file bytes**.
- The digest is computed over the file **exactly as stored**: pixels, container/encoding
  bytes, and **all metadata are part of the identity**. No decoding, no normalisation. Two
  files that differ in **any** byte — pixels, encoding, or metadata — hash to a **different**
  value.
- This hash is the **join key** between the local set and the remote set in the sync
  algorithm (`design-algorithm.md`).
- **Consequence:** any change to the file (pixels **or** metadata **or** encoding — e.g.
  re-rating, re-encoding, or moving to a folder that rewrites tags) changes the hash. The old
  identity therefore **disappears** (`DELETE`) and the new one **appears** (`UPLOAD`); a
  file-level change is a delete+upload, **not** a `MOVE`. A `MOVE` is emitted only when the
  **same** file bytes map to **different** records (see `design-algorithm.md` §4).
- The **server** computes the same hash on collect by downloading the full asset bytes via
  `GET /assets/{id}` and hashing them — no decoding (see `design-api.md`).
- No pixel-buffer normalisation is needed: the digest is over the literal file bytes, so
  client and server agree as long as Immich stores and returns the uploaded original
  unchanged (the invariant below). Multi-frame / preview bytes are part of the file and are
  included automatically.

### Invariants (client and server must both hold)
- **Stored-bytes immutability:** `client_hash == server_hash` holds only if the bytes Immich
  stores and returns via `GET /assets/{id}` are **byte-for-byte identical** to the client's
  final file. This is true only if Immich stores the uploaded **original** without
  re-encoding / re-serialisation and we always fetch the **original** (not a
  preview/thumbnail). If Immich ever re-encodes or re-saves the original, every photo would
  look "new + deleted" each sync. **Verify against the target Immich version before relying
  on it.**
- **Deterministic (idempotent) metadata write:** the upload-preparation write must be
  idempotent — preparing the same file twice yields **byte-identical** output — so the local
  hash is stable across runs and re-syncs. (The client hashes the prepared file and uploads
  that same file, so the local and upload hashes match by construction; the stored-bytes
  invariant above is what makes the *remote* hash match on the next collect.)

## Supported formats
- **Supported:** jpeg, tiff, png.
- **Not supported:** RAW and any other format, unless the metadata write path is cheap and
  already supported by the current tooling. (Content hashing needs no decoding, so format
  support is gated only by metadata write-back.)
- `TODO`: fix the accepted-extension list in the client (the test file currently lists many
  extensions, e.g. RAW, which are out of scope) to exactly the supported set.

## Open items (tracked)
- `TODO` `write_to_file` date-time guarantee (step 4 above).
- `TODO` narrow the accepted extension list to the supported formats.
