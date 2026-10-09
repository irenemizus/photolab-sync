# Photolab Sync — High-Level Design

## Description
Photolab Sync is an automated synchronization system between a local directory tree of
photos (jpeg/tiff/png) and an Immich database.

- A **client machine** holds the local directory tree (the "source of truth").
- A **server node** hosts an Immich backend and the SyncServer.

The guiding principle: **the SyncClient is the brain** (it decides *what* to do) and
**the SyncServer is a mostly dumb translator** (it only *applies* operations against
Immich through the Immich API). Immich itself is treated as an opaque black box; we never
rely on its internal implementation details.

## Architecture

### Main blocks
1. **Immich backend** — used as a black box via the Immich API, on the server node.
2. **SyncServer** — the server part of this project, on the server node (same node as
   Immich). Talks to Immich over the Immich API and to the client over a custom REST API.
3. **SyncClient** — the client part of this project, on the client machine that owns the
   local photo tree. Computes the synchronization plan and drives the session.

### Interaction between the blocks
- Immich backend ↔ SyncServer: **Immich API**.
- SyncClient ↔ SyncServer: **custom REST API** (see `design-api.md`).

### Block responsibilities
#### SyncClient (the "smart" side)
- Scans the local tree, prepares each image's final metadata (see `design-metadata.md`),
  and computes a content hash (SHA512 over the **pixel bit-stream**, metadata stripped).
- Validates the local set (no duplicate content across events — a fatal pre-check).
- Builds the **ordered** list of atomic operations that reconciles local state with remote
  state (see `design-algorithm.md`).
- Drives the session: start → collect + poll → apply operations in order → stop.

#### SyncServer (the "dumb translator")
- Manages a single active sync session via a one-time key with a short inactivity TTL.
- On collect: reads **every** asset from Immich, downloads its full bytes via
  `GET /assets/{id}` one by one, and computes the **same content SHA512** over the
  **pixel bit-stream** (metadata stripped); reconstructs each asset's 7-level position
  from its embedded metadata;
  returns `{hash → position}` pairs. **No caching** — the full set is re-downloaded and
  re-hashed on every sync.
- Translates each client atomic operation into the corresponding Immich API call, applying
  them strictly in the client-given order.

## High-level flow
1. Client prepares `local_pairs = {content_hash → 7-level position}` and validates it.
2. Client → `POST /v1/sync/start` → receives a one-time key.
3. Client → `POST /v1/collect` → `202` (collection job started).
4. Client polls `GET /v1/collect/status` → `202` + `progress` (0.0–1.0) … → `200` + `remote_pairs`.
5. Client computes the ordered operation list locally from `local_pairs` + `remote_pairs`.
6. Client applies each operation in order (one short request per operation).
7. Client → `POST /v1/sync/stop` → key invalidated, session released.

## Key design decisions
- **Immich is a black box.** We use its public API only; nothing depends on internals.
- **Content identity = SHA512 of the pixel bit-stream** (encoded pixels, metadata
  stripped). A metadata-only change does **not** change the identity (→ MOVE); a pixel
  or encoding change does (→ DELETE + UPLOAD). Full bytes are re-downloaded and
  re-hashed on every sync. **No caching** anywhere (for now).
- **Metadata is flattened into the file.** The 7-level layout exists *only* on the client;
  before upload the place is merged into the image's metadata (see `design-metadata.md`).
  Immich only ever sees "pixels + metadata", never the original file path.
- **The client is the source of truth.** Deleting remote-only photos is automatic, 
  but before synchronization that involves deleting of remote images, ask user for confirmation.
- **Only one client / one collection at a time.** Enforced with an atomic session lock.
- **A crashed sync is resumed by simply re-running it.** Operations are idempotent; a fresh
  collect makes a re-run converge.
- **A server restart mid-session is fatal.** All in-flight state (key, collect job,
  operation progress) is lost by design. The client aborts and the user starts a new sync.
- **No long blocking HTTP calls.** Long work (collect) is split into "start (202)" + "poll
  status". The session key has a ~1 minute inactivity TTL so a crashed client never holds
  the lock forever.
- **No security for now** beyond an opaque one-time key; security is to be designed
  separately.
- **No RAW formats.** jpeg, tiff, png (plus any other format only where the metadata write
  path is already supported).

## Supported formats
jpeg, tiff, png. No RAW. Additional formats are added only if the metadata write path is
  cheap and the metadata tooling already supports them. (Content hashing strips metadata
  from the encoded bit-stream — no decoding — so format support is gated only by metadata
  write-back.)

## Detailed documents
- `design-metadata.md` — the Place/Metadata model, the per-image upload preparation
  pipeline, content (file-byte) hashing, and supported formats.
- `design-algorithm.md` — the synchronization algorithm: inputs, phases, operation
  ordering, album identity & same-name collisions, duplicate/empty-album handling, corner
  cases, and idempotency.
- `design-api.md` — the SyncServer REST API: endpoints, request/response schemas, the
  collect/status/poll flow, session-key lifecycle, concurrency & lock atomicity, timeouts,
  and the failure model.
