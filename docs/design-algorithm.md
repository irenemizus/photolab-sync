# Synchronization Algorithm — Low-Level Spec

## Scope
This document specifies the algorithm that reconciles the **local** photo set with the
**remote** (Immich) set and produces the **ordered** list of atomic operations the SyncServer
must apply. This is the intended behaviour of `algorithm_builder.build_sync_algorithm`, which
is part of the **SyncClient engine**: it defines *what* operations to run and *in what
order*; the SyncClient is the decision maker and the SyncServer only executes them
(`design.md`, `design-api.md`).

The algorithm is pure: it takes two maps and returns an ordered list. No side effects, no
network access — so it is trivially testable.

Section numbers here are stable and are referenced from code comments (`§…`).

## 1. Inputs and the record model

### 1.1 The pairs
The algorithm takes two maps (a "structure" is one of these maps):

```
local_pairs:  { content_sha512_hex : record }
remote_pairs: { content_sha512_hex : record }
```

- **Key** = the photo's **content hash**: SHA512 over the **pixel bit-stream**
  (encoded pixels, metadata stripped; see `design-metadata.md`).
- **Value** = a **record** describing where the photo lives and its managed metadata
  (§1.2). The current code uses a *path string* as the value; that is a simplification
  (§1.3).

### 1.2 The record (the value): place + full date-time
A **record** is a mapping that carries the photo's managed metadata, at minimum:

| Field                   | Source                                            |
|-------------------------|---------------------------------------------------|
| `year`, `month`         | place (levels 1–2) / `date_time_original`          |
| `event`, `subevent`     | place (levels 3–4) / XMP `Event`                   |
| `category`              | place (level 5) / IPTC+XMP                          |
| `supplemental_category` | place (level 6) / IPTC+XMP                          |
| `rating`                | place (level 7) / XMP `Rating`                      |
| `date_time_original`    | full `Exif.Photo.DateTimeOriginal` (day + time)     |

- **Album identity** = `(year, month, event, subevent)` — the first 4 fields.
- **Position** = all 7 place fields (levels 1..7).
- The record is the **unit of comparison** for MOVE detection: two records differ in
  **any** field (including `date_time_original`) ⇒ the photo must be MOVED (§4).
- The **7-level path** is a *projection* of the record (it drops the day/time of
  `date_time_original`). The file name is **not** part of the record and never is.

### 1.3 Why "hash → path" is only a simplification
The current code values the map with the full path string
`year/month/event/subevent/category/supplemental/rating/FILENAME`. Two problems:

1. The **file name** is irrelevant to the algorithm and is **unavailable on the remote
   side** (the server reconstructs positions from metadata, which carries no file name).
2. The path only encodes `year/month`, **not** the day/time of `date_time_original`. A
   metadata-only change that stays in the same folder (e.g. the day/time within the same
   month changes) is invisible in the path but **is** a metadata change that must be a
   MOVE (§4). A path-based comparison therefore misses it.

Hence the value must be the full **record** (§1.2), not the path. Until the record is
implemented, path comparison is a best-effort approximation.
`TODO`: switch `build_sync_algorithm` to compare records (place + full `date_time_original`).

## 2. Building the maps

### 2.1 Local
- Walk the tree; for each supported image run the upload-preparation pipeline
  (`design-metadata.md`) to obtain (a) the content hash and (b) the **record** (the
  place parsed from the path, plus the file's `date_time_original` and managed metadata).
- Collect into `local_pairs`.

### 2.2 Remote (from the collect)
- The server reads every asset, downloads its full bytes (`GET /assets/{id}`), hashes them,
  and reconstructs the **record** from the asset's **embedded metadata**
  (`year/month`/day/time from `date_time_original`; `event/subevent/category/supplemental/
  rating` from XMP/IPTC).
- The result is returned to the client as `remote_pairs` (`design-api.md §6.2`).
- Because the metadata carries the 7 levels, the server can reconstruct records even
  though it has no idea of the original file path.

## 3. Album identity and reconciliation
Albums are identified by their 4-level identity `year/month/event/subevent`. The display
name is `event — subevent`. **Multiple albums may share a display name** (different
`year/month`).

### 3.1 Canonical identity
- **Identity** = `(year, month, event, subevent)`. Two albums are equal if all four match.
- **Display name** (the Immich album name) = `event — subevent`.
- Immich **allows multiple albums with the same display name**. SyncServer must **not**
  assume "one name → one album"; it must track **identity → Immich album id** and handle
  several ids sharing a name.
- The server reconstructs each album's identity during collect from the `year/month` in
  its assets' `date_time_original` plus the `event/subevent` in their XMP.
  - `TODO`: confirm the server-side reconstruction stays unambiguous for a merged
    (New-Year, §3.3 Case B) album that legitimately holds photos from two `(year,month)`;
    the anchor rule in §3.3 defines which `(year,month)` names such an album.

### 3.2 Create / Delete / Rename
Let `local_albums` = set of 4-level identities over `L`; `remote_albums` = set over `R`.

- **CREATE_ALBUM**: a local identity **not** in `remote_albums` **and** with ≥ 1 photo.
  (Empty local albums are ignored — §8.)
- **DELETE_ALBUM**: a remote identity **not** in `local_albums` and **not** renamed,
  emitted only once its surviving photos have been moved out (moves run before deletions,
  §6.1). Empty remote albums are **kept as-is** (not deleted) — §8.
- **RENAME_ALBUM**: a remote identity missing locally whose **photos** now live in a
  local identity that is "close" → rename. The intended mechanism is a greedy best-match:
  score each candidate local album by a similarity ratio, sort best-first, commit so no
  album is renamed twice and no two rename to the same target name.
  - `TODO`: with `year/month` now part of the identity, match on the **renameable**
    `event/subevent` part (the display name), not the full 4-level identity; a pure
    `year/month` change is not a rename. Also, a rename changes the identity, so the
    server must update its identity→id mapping (`design-api.md §6.3`).

### 3.3 Same-name collisions and the "New Year" case
Two client folders can share `(event, subevent)` but differ in `(year, month)`. This is the
core collision. **Choosing between the two options is an open product decision**; both are
specified.

- **Case A — split events (default, deterministic).** Each `(year,month,event,subevent)`
  is its **own** album. Same display name, different identities → **two** albums. Fully
  determined by the folder structure. A real event spanning a year boundary shows up as
  **two** albums.
- **Case B — "New Year" single event.** A single logical event that starts at the end of
  one year and continues into the next (e.g. Dec 31 2025 → Jan 1 2026) is **one** album.
  - **Merge rule (proposed):** merge two local folders that (a) share `(event, subevent)`,
    (b) have `(year,month)` = (December of year Y) and (January of year Y+1), and (c) whose
    photos' `date_time_original` form a **contiguous** span actually straddling the
    Dec 31 / Jan 1 boundary.
  - **Identity anchor:** a merged album still needs a single 4-level identity. Anchor it
    to one `(year,month)` (e.g. the December folder, or the folder with more photos). The
    album may then hold photos whose own metadata `year/month` is January of the next year
    — that is fine, since metadata is per-photo.
  - **Consequence:** the "identity = folder `(year,month)`" invariant is relaxed for merged
    albums; the album is anchored to one `(year,month)` but legally contains an adjacent
    month's photos. The algorithm treats the merged album as **one** identity on both sides.
  - Merging (Case B) must happen **before** the album grouping runs, so the grouping sees
    one logical album rather than two boundary-adjacent folders.
  - `TODO`: decide which case to implement (or both behind a policy switch) and pin the
    merge rule + anchor rule. Until decided, **Case A is the safe default.**

## 4. MOVE semantics
A **MOVE** is emitted for a content hash (i.e. **identical pixel bit-stream**) present on
**both** sides when the two **records** differ in any field (§1.2):
`MOVE(hash, from=remote_record, to=local_record)`.

Because the content hash is over the **pixel bit-stream** (metadata stripped,
`design-metadata.md`), a change to the **pixels** or the **encoding** (e.g. JPEG↔PNG)
yields a **new** hash. Such a change is a `DELETE` (old hash, §5.2) + `UPLOAD` (new hash,
§5.1), **not** a MOVE. In particular:

- **Pixel change** (re-photograph, edit, re-encode to a different format): new hash
  ⇒ `DELETE` + `UPLOAD`.
- **Metadata-only change** (re-rating, re-filing into a folder that rewrites tags, a
  day/time change within the same month): the pixel bit-stream is unchanged ⇒ the hash
  is unchanged ⇒ `MOVE` (the server updates the asset's stored metadata in Immich;
  no file re-upload).

A MOVE therefore fires whenever **identical pixels** carry **different** records. The
server is responsible for applying the metadata update: it fetches the asset from
Immich, rewrites its metadata to the target record, and saves it back
(`design-api.md §6.3`).

## 5. Reconciling content (the phases)
Let `L = local_pairs`, `R = remote_pairs`. Work on copies so the originals stay for
reporting.

### 5.1 New content → UPLOAD
- `new = set(L.keys()) - set(R.keys())`
- For each `h in new`: emit `UPLOAD(h, record=L[h])`.

### 5.2 Removed content → DELETE (and server-duplicate cleanup)
- `gone = set(R.keys()) - set(L.keys())`
- For each `h in gone`: emit `DELETE(h)`.
- **Remote-only photos are deleted first and unconditionally.** The client is the ideal /
  source of truth; the user has no qualified per-photo decision and is always assumed to
  answer "yes". No confirmation prompt.
- **Server-side duplicates:** if `R` holds the **same** content hash under **two or more**
  assets, keep **one** copy and emit `DELETE` for the **extra** copies (targeted by the
  server-side `asset_id`, not just the hash). Net effect after the sync: **exactly one**
  copy of a duplicated hash survives.
  - Mechanism: the collect result must expose the extra copies
    (`design-api.md §6.2`, "duplicate encoding").
  - `TODO`: finalize how collect reports "same hash, N assets" (recommend: primary record
    keyed by hash + a list of extra `asset_id`s to delete).

### 5.3 Common content, differing record → MOVE
- For each `h in set(L.keys()) & set(R.keys())`:
  - if `L[h] != R[h]` (records differ, §4): emit `MOVE(h, from=R[h], to=L[h])`.
  - else: no operation (already consistent).
- Note: a pixel-changed file does **not** reach this step — it has a new hash
  and is handled as `DELETE` + `UPLOAD` (§4). A **metadata-only** change keeps the
  same hash and **does** reach this step, producing a MOVE.
- After steps 5.1–5.3, the two "remaining" sets (hashes present on both sides) are equal in
  size; the current assertion `len(remaining_local) == len(remaining_remote)` is correct and
  must be kept.

## 6. The operation plan

### 6.1 Ordering
The order is part of the contract (the client defines it). It must guarantee every
operation's preconditions hold when it runs:

1. **DELETE** (removed photos) — first; the client is authoritative and this frees
   resources early.
2. **RENAME_ALBUM** — before moves, so moves can target the renamed album.
3. **CREATE_ALBUM** — before moves, so moves can target newly created albums.
4. **MOVE** — after the target albums exist; each move also rewrites the photo's metadata.
5. **DELETE_ALBUM** — after surviving photos have been moved out, so only albums that are
   empty (or to be removed) are deleted.
6. **UPLOAD** — new photos; each upload places the photo into its album (created in step 3
   if needed) and writes its metadata.

This matches the current return order:
`deletions + renames + creations + moves + album_deletions + uploads`.

### 6.2 Idempotency / crash recovery
A crashed sync is resumed by **re-running it from the start** (a fresh collect recomputes
`remote_pairs`, so the plan converges). Therefore **every operation must be idempotent or a
no-op on re-application**:

| Operation            | Re-application behaviour                                   |
|----------------------|------------------------------------------------------------|
| `UPLOAD`             | no-op if the hash is already present (or a `MOVE` if the record differs) |
| `DELETE`             | if the target is already gone → **non-fatal** domain error, logged; client re-runs the sync from the beginning (`design-api.md §3`) |
| `MOVE`               | no-op if the photo is already at the target record          |
| `CREATE_ALBUM`       | no-op if the identity already exists                        |
| `DELETE_ALBUM`       | no-op if the album is already gone / empty                   |
| `RENAME_ALBUM`       | no-op if the album already has the target name               |

## 7. Pre-validation (before the plan is produced)
The client validates `local_pairs` **before** running the algorithm. A validation failure
is a **general error** reported to the user and **aborts the sync** — "before any
synchronization is even started". Nothing is applied.

- **No duplicate content / no photo in two events.** A single content hash MUST map to
  **exactly one** record. If two local files produce the same hash but **different**
  records (in particular different events), this is a **FATAL** error; no duplicates are
  tolerated and the sync does not start.
  - `TODO`: also define the behaviour when the same hash appears twice under the **same**
    record (same event, different category/rating/…); treat as an error too.
- **Server-side duplicates** are **not** a pre-check; they are resolved during the plan
  (§5.2), because a server-side duplicate must not by itself abort a sync.

## 8. Corner cases (summary)
- **New-year boundary event** → Case A (two albums) or Case B (one merged album); open
  decision (§3.3).
- **Same-name albums** → multiple, disambiguated by identity (`year/month`); server tracks
  identity→id (§3.1).
- **Metadata-only change** (incl. day/time within the same month, re-rating, re-filing)
  → pixel bit-stream unchanged → same hash → `MOVE` (server updates metadata, §4).
- **Pixel-only change, same position** → `DELETE`(old hash) + `UPLOAD`(new hash); **not** a
  move.
- **Server photo with no event metadata but its hash is local** → `MOVE` (it gets assigned
  to its local album/record). The client model has no "photo without event", so such a
  photo is only ever *moved into* a valid position, never created.
- **Server photo with no event and its hash NOT local** → remote-only → `DELETE`.
- **Local duplicate** (same hash, two records) → FATAL pre-check, abort before applying (§7).
- **Server duplicate** (same hash, two+ assets) → keep one, `DELETE` the rest (§5.2).
- **Local empty album** → ignore + log. **Remote empty album** → keep + log.
  - Precedence: an empty remote album is **always kept** (the empty rule wins over the
    "identity absent locally" rule). `TODO`: confirm this precedence.
