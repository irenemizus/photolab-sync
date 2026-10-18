"""ImmichClient — a thin wrapper over the public Immich REST API (v3).

The only module in the project that talks to Immich, and it is used only by the
SyncServer (sync_server.py). No other module is aware of Immich's existence.

The endpoints used here were verified against a running Immich v3.0.3. The
differences from the v1/v2 API that matter here:
  * the base path is ``/api`` (there is no ``/v1`` segment),
  * auth is the ``x-api-key`` header (v1/v2 used ``Authorization: <key>``),
  * ``GET /timeline/bucket`` returns a *columnar* response (one array per field).

Only the public API is used — no DB, no admin endpoints (docs/design.md).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# (connect, read) — reads can stream a whole original file.
DEFAULT_TIMEOUT = (5.0, 300.0)


def _iso_utc(dt: datetime) -> str:
    """ISO 8601 with an explicit UTC zone — Immich's upload validation rejects
    naive datetimes (e.g. ``2026-09-05T10:20:30.000Z``)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ImmichError(Exception):
    """A non-2xx response from the Immich API."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Immich API {status}: {message}")
        self.status = status
        self.message = message


class ImmichClient:
    def __init__(self, base_url: str, api_key: str):
        url = base_url.rstrip("/")
        if not url.endswith("/api"):
            url += "/api"
        self.api_url = url
        self._http = requests.Session()
        self._http.headers["x-api-key"] = api_key

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        resp = self._http.request(method, f"{self.api_url}/{path.lstrip('/')}", **kwargs)
        if not resp.ok:
            try:
                message = str(resp.json().get("message", resp.text))
            except ValueError:
                message = resp.text
            raise ImmichError(resp.status_code, message) from None
        return resp

    def _get_json(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs).json()

    # -- server ------------------------------------------------------------

    def get_version(self) -> tuple[int, int, int]:
        v = self._get_json("server/version")
        return (int(v["major"]), int(v["minor"]), int(v["patch"]))

    # -- assets ------------------------------------------------------------

    def list_month_buckets(self) -> list[tuple[str, int]]:
        """Every month that has assets: ``[(timeBucket ISO, count), ...]``."""
        return [(b["timeBucket"], int(b["count"]))
                for b in self._get_json("timeline/buckets")]

    def list_asset_ids_in_bucket(self, time_bucket: str) -> list[str]:
        """The asset ids in one month (the columnar timeline response, ``id`` column)."""
        columns = self._get_json("timeline/bucket", params={"timeBucket": time_bucket})
        return [str(asset_id) for asset_id in columns.get("id", [])]

    def get_asset(self, asset_id: str) -> dict:
        return self._get_json(f"assets/{asset_id}")

    def download_original(self, asset_id: str, dest: Path) -> Path:
        """Stream the original file (the exact stored bytes) to ``dest``."""
        with self._request("GET", f"assets/{asset_id}/original", stream=True) as resp, \
                open(dest, "wb") as out:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    out.write(chunk)
        return dest

    def upload(self, file_path: Path | str,
               album_id: str | None = None,
               file_created_at: datetime | None = None,
               file_modified_at: datetime | None = None,
               is_favorite: bool = False) -> dict:
        """Multipart asset upload; returns the created asset object.

        The form carries ``fileCreatedAt``, ``fileModifiedAt``, ``isFavorite``
        and the file under ``assetData``. The ``albumId`` param is accepted for
        API compatibility but Immich v3 silently ignores it — callers must
        attach the asset to the album explicitly via add_assets_to_album.
        The file's own Exif ``DateTimeOriginal`` is what Immich indexes for
        the timeline.
        """
        file_path = Path(file_path)
        now = datetime.now(timezone.utc)
        params = {"albumId": album_id} if album_id else None
        data = {
            "fileCreatedAt": _iso_utc(file_created_at or now),
            "fileModifiedAt": _iso_utc(file_modified_at or now),
            "isFavorite": "true" if is_favorite else "false",
        }
        with open(file_path, "rb") as fh:
            files = {"assetData": (file_path.name, fh)}
            resp = self._request("POST", "assets", params=params, data=data, files=files)
        return resp.json()

    def delete_asset(self, asset_id: str) -> None:
        """Delete one asset. v3 exposes only the bulk endpoint; an unknown id
        is reported with 400 ("Not found or no asset.delete access").

        Deletion is soft: the asset lands in the trash, hidden from the
        timeline but still indexed by content — re-uploading its exact bytes
        answers ``duplicate``.
        """
        self._request("DELETE", "assets", json={"ids": [asset_id]})

    def restore_trashed_asset(self, asset_id: str) -> None:
        """Bring a trashed asset back into the timeline (public API only
        offers restore-all / restore-by-id / empty-all, no single purge)."""
        self._request("POST", "trash/restore/assets", json={"ids": [asset_id]})

    # -- albums ------------------------------------------------------------

    def list_albums(self, asset_id: str | None = None) -> list[dict]:
        """All albums, or (with ``asset_id``) the albums containing that asset."""
        params = {"assetId": asset_id} if asset_id else None
        return self._get_json("albums", params=params)

    def create_album(self, name: str) -> dict:
        return self._request("POST", "albums", json={"albumName": name}).json()

    def update_album(self, album_id: str, name: str | None = None,
                     description: str | None = None) -> dict:
        dto: dict[str, Any] = {}
        if name is not None:
            dto["albumName"] = name  # v3: "name" is accepted but silently ignored
        if description is not None:
            dto["description"] = description
        return self._request("PATCH", f"albums/{album_id}", json=dto).json()

    def delete_album(self, album_id: str) -> None:
        self._request("DELETE", f"albums/{album_id}")

    def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        self._request("PUT", f"albums/{album_id}/assets", json={"ids": list(asset_ids)})

    def remove_assets_from_album(self, album_id: str, asset_ids: list[str]) -> None:
        self._request("DELETE", f"albums/{album_id}/assets", json={"ids": list(asset_ids)})
