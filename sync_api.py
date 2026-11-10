"""Shared types and constants of the SyncServer REST API (docs/design-api.md).

Both server implementations — sync_server.py (the real one) and
stub_sync_server.py (the development stub) — speak this API; this module
holds the types and constants they share.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Response:
    """An endpoint handler's result: the HTTP status code and the JSON payload
    to send back (design-api.md §6)."""
    status: int
    payload: dict


class ApiError(Exception):
    """An error to send back to the client: the HTTP status, the stable error
    code (design-api.md §4) and a human-readable message."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} [{code}]: {message}")
        self.status = status
        self.code = code
        self.message = message


# Session inactivity TTL (design-api.md §2): sliding, refreshed on every
# authenticated request.
KEY_TTL_SECONDS = 60
