# SyncServer REST API — Low-Level Spec

## Scope
This document specifies the custom REST API between **SyncClient** and **SyncServer**:
conventions, endpoints, request/response schemas, the collect/status/poll flow, the
session-key lifecycle, concurrency & lock atomicity, timeouts, and the failure model.

Reminders from the high-level design (`design.md`): the client is the decision maker and the
server only executes; **no caching** on the server; **no long blocking HTTP calls**; **no
security** beyond an opaque one-time key (security to be designed separately).

Section numbers are stable and are referenced from code comments (`§…`).

## 1. Conventions
- **Transport:** plain HTTP for now. No TLS. The one-time key is an opaque random token; it
  is returned in the `sync/start` response body and sent in a request header on all
  subsequent calls (e.g. `Authorization: <key>`). No security of any kind for now.
- **Auth:** the key is **required on every endpoint except `sync/start`**.
- **Status codes:** in-progress work → `202 Accepted` (body carries the in-flight state,
  including `progress`); terminal success → `200`; domain errors → `4xx`/`5xx` with a JSON
  error body.
- **No long blocking calls.** Any potentially long operation is split into "start (202)" +
  "poll status" so a single request never blocks.
- **Single actor:** at most one client synchronizes at a time, and at most one collection
  process at a time. All required checks and failure reports are defined in §3–§5.

## 2. Session-key lifecycle
- **Created** by `sync/start` (§6.1); **required** on every other endpoint.
- **Inactivity TTL ≈ 60 s:** the key expires if the client makes no request within ~1
  minute. Each authenticated request **refreshes** the TTL (sliding expiry). This bounds how
  long a crashed client holds the lock.
- **On expiry**, the session slot returns to `idle` (a new `sync/start` is then accepted).
- **Explicitly cleared** by `sync/stop` (§6.4).
- Because the TTL is short and we never make long blocking calls, a stuck or long request
  cannot hold the session indefinitely.
- The key value is returned in the response **body** over plain HTTP (no security for now).

## 3. Failure model
- **Server restart mid-session:** all in-flight state — the active key, the collect job, and
  per-operation progress — is **lost**. This is an **expected fatal condition**. The client,
  on repeated connection failures (after a small number of retries), **stops the sync** and
  reports to the user that the server was lost. When the server is back up, the user simply
  starts a **new** sync (which re-collects and re-converges).
- **No persistence** of mid-session server state is attempted.
- **Mid-sync "already deleted"** (a `DELETE`/`MOVE` target that no longer exists): a
  **non-fatal** domain error (`already_deleted`, §4). It is reported to the client, **logged
  to the user**, and the client **re-runs the sync from the very beginning** (it indicates a
  possible transient server anomaly mid-sync). Not fatal.
- **Client-side validation failure** (e.g. local duplicate content, `design-algorithm.md §7`):
  **fatal** — the sync aborts *before* any operation is applied.

## 4. Error reporting (JSON)
All error responses carry:
```json
{ "code": "<machine-readable>", "message": "<human readable>" }
```
Codes (non-exhaustive):

| Code                    | HTTP   | Meaning / action                                                        |
|-------------------------|--------|--------------------------------------------------------------------------|
| `invalid_key`           | 401    | missing/expired/wrong key                                                 |
| `session_conflict`      | 409    | `sync/start` while a session is active                                    |
| `collection_in_progress`| 409    | `collect` while a collection is already running                          |
| `no_collection`         | 404    | `collect/status` polled without a started collect                        |
| `already_deleted`       | 409/410| target no longer exists — **non-fatal**, log + re-run the sync (§3)       |
| `server_error`          | 500    | unexpected failure translating to Immich                                  |

- **Fatality rule:** a client-side validation failure is fatal (abort before applying); a
  mid-sync `already_deleted` is non-fatal (log + re-run); a server-down is fatal (abort and
  tell the user).
- `TODO`: finalize the exact code set and map each to fatal vs non-fatal.

## 5. Concurrency invariants
- At most **one** active session (enforced atomically by `sync/start`, §6.1).
- At most **one** collection job at a time (within the session, §6.2).
- Operations are applied **in the order the client sends them**; the client is the only
  thing that decides that order (`design-algorithm.md §6.1`).

## 6. Endpoints

### 6.1 `POST /v1/sync/start`
- **Auth:** none (this endpoint *creates* the key).
- **Purpose:** begin a sync session.
- **Request body:** empty (or a small client descriptor).
- **Response:** `200`
  ```json
  { "key": "<one-time key>", "key_ttl_seconds": 60 }
  ```
- **Concurrency / atomicity (IMPORTANT):** exactly one session may be active. The "active
  session" is a **single atomic state slot**. `sync/start` must atomically transition
  `idle → active` (compare-and-set under a server lock / single-writer). If a session is
  already active, respond `409 session_conflict`.
  - **Two racing `sync/start` requests:** exactly **one** wins; the other gets `409`. This
    must be race-free under concurrent requests (mutex / CAS on the session slot — never a
    read-then-write without holding the lock).
  - `TODO`: define the exact server-side primitive (e.g. a locked `active_session` slot)
    and its behaviour if the winning client never sends `sync/stop` (covered by the TTL).

### 6.2 `POST /v1/collect` and `GET /v1/collect/status`
- **`POST /v1/collect`** (auth: key). Ask the server to (re)compute the remote set: list
  **every** asset from Immich, download each via `GET /assets/{id}` one by one, compute the
  content SHA512 over the raw file bytes, and reconstruct the **record** from its metadata.
  - **Concurrency:** **only one** collection may run at a time. If one is already running,
    respond `409 collection_in_progress`.
  - **Response:** `202`
    ```json
    { "job": "<id>", "status": "running", "progress": 0.0 }
    ```
  - No full result is returned here. **No caching**: a new `collect` always re-downloads
    and re-hashes everything.
- **`GET /v1/collect/status`** (auth: key). Poll the in-flight collection.
  - while running → `202`:
    ```json
    { "status": "running", "progress": <0.0..1.0> }
    ```
  - when done → `200`:
    ```json
    {
      "status": "done",
      "result": {
        "<sha512_hex>": { "year": 2026, "month": 4, "event": "…", "subevent": "…",
                          "category": "…", "supplemental_category": "…", "rating": 3,
                          "date_time_original": "2026-04-05T10:20:30" }
      },
      "extra_copies": [ { "asset_id": "<id>", "hash": "<sha512_hex>" } ]
    }
    ```
  - if no collection was started for this session → `404 no_collection` (client should call
    `collect` first).
  - `progress` is a coarse fraction in `0.0..1.0` of the assets processed so far.
  - **Duplicate encoding:** `result` maps each hash to the **primary** (kept) record. Any
    **additional** assets sharing a hash are listed in `extra_copies` (with the server-side
    `asset_id`) so the client can emit `DELETE` for them (`design-algorithm.md §5.2`).
    `TODO`: confirm this shape; the alternative is to report each asset with an `asset_id`
    and let the client pick the one to keep. Recommend the `extra_copies` form.

### 6.3 Atomic operations (one request each, applied in client-given order)
The client sends these **strictly in the order** the algorithm produced
(`design-algorithm.md §6.1`). Photos are referenced by **content hash** (or `asset_id` for
duplicate copies); albums by their **4-level identity** (`year/month/event/subevent`). The
server resolves a hash → Immich asset id using its collect-time mapping.

Each returns `200` with a small result on success, or a JSON error otherwise.

- **`POST /v1/upload`**
  - **Body:**
    ```json
    {
      "hash": "<sha512>",
      "album": "<year>/<month>/<event>/<subevent>",
      "record": { "…full record…": "§1.2 of design-algorithm.md" },
      "content_type": "<mime>",
      "file": "<bytes>"
    }
    ```
    (or a multipart body carrying the file). The uploaded artifact is the client's **final
    file** — pixels + augmented metadata (`design-metadata.md`).
  - **Server:** ensure the `album` exists, upload the bytes to Immich, set the asset's
    metadata to the fields implied by the record, attach it to the album.

- **`POST /v1/delete`**
  - **Body:** `{ "hash": "<sha512>" }` **or** `{ "asset_id": "<id>" }` (the latter targets
    one of several duplicate copies).
  - **Server:** find the asset (collect-time mapping) and delete it from Immich.
  - **If already gone:** respond `409/410 already_deleted` — **non-fatal**; the client logs
    it and re-runs the sync from the beginning (§3).

- **`POST /v1/move`**
  - **Body:**
    ```json
    {
      "hash": "<sha512>",
      "album": "<target 4-level identity>",
      "record": { "…target full record…": "" },
      "refreshed_file": "<optional bytes>"
    }
    ```
  - **Server:** move the asset into the target album and **rewrite** its metadata to the
    target record. A MOVE always rewrites metadata, even if the album is unchanged
    (`design-algorithm.md §4`). When only metadata changed (same album/path), the payload
    carries `refreshed_file` (the final file with the updated metadata) so the server can
    update the asset's stored metadata.

- **`POST /v1/album/create`**
  - **Body:** `{ "identity": "<4-level>", "name": "<event> — <subevent>" }`.

- **`POST /v1/album/delete`**
  - **Body:** `{ "identity": "<4-level>" }`. (Only ever called for albums that will be empty.)

- **`POST /v1/album/rename`**
  - **Body:** `{ "identity": "<4-level>", "new_name": "<event> — <subevent>" }`.
  - A rename changes the `event/subevent` part, hence the identity; the server updates its
    `identity → album_id` mapping accordingly (`design-algorithm.md §3.2`).

### 6.4 `POST /v1/sync/stop`
- **Auth:** key.
- **Purpose:** end the session.
- **Response:** `200`. The key is **invalidated immediately** and the session slot returns
  to `idle`, so another client may `sync/start`.
