"""Content hash: SHA512 over the raw file bytes.

See docs/design-metadata.md, "Content hashing (image identity)":

- The digest is computed over the file exactly as stored — pixels, container/encoding
  bytes, and ALL metadata are part of the identity. No decoding, no normalisation. Two
  files that differ in any byte hash to a DIFFERENT value.
- This hash is the join key between the local and the remote set
  (docs/design-algorithm.md §1). A file-level change (pixels, metadata, or encoding)
  changes the hash — the old identity is DELETEd and the new one UPLOADed (§4).
- The server computes the same hash on collect by downloading the full asset bytes and
  hashing them (no decode) — so client and server agree iff Immich stores and returns
  the uploaded original byte-for-byte (design-metadata.md invariants).
"""

import hashlib


def compute_content_hash(data: bytes) -> str:
    """SHA512 hex digest of the raw file bytes."""
    return hashlib.sha512(data).hexdigest()
