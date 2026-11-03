"""Shared types of the SyncServer REST API (docs/design-api.md).

Both server implementations — sync_server.py (the real one) and
stub_sync_server.py (the development stub) — speak this API; this module
holds the types they share.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Response:
    """An endpoint handler's result: the HTTP status code and the JSON payload
    to send back (design-api.md §6)."""
    status: int
    payload: dict
