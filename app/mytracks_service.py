"""HTTP client helpers for pulling roster and geofence exports from My Tracks."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

_PARTICIPANTS_EXPORT_PATH = "/api/v1/export/participants"
_GEOFENCES_EXPORT_PATH = "/api/v1/export/geofences"
_REQUEST_TIMEOUT_S = 30.0


class MyTracksSyncError(ValueError):
    """Raised when My Tracks export HTTP or payload parsing fails."""


def normalize_mytracks_base_url(domain: str) -> str:
    """Return a canonical base URL for My Tracks admin export calls."""
    trimmed = domain.strip().rstrip("/")
    if trimmed == "":
        raise MyTracksSyncError("Expected My Tracks domain, got empty value")
    if not trimmed.startswith(("http://", "https://")):
        trimmed = f"https://{trimmed}"
    parsed = urlparse(trimmed)
    if parsed.netloc == "":
        raise MyTracksSyncError(f"Expected My Tracks domain, got {domain!r}")
    return trimmed


def sync_geofences_from_my_tracks(
    *,
    base_url: str,
    password: str,
    username: str,
) -> int:
    """Fetch geofence export JSON and return the number of geofences."""
    payload = _fetch_export_json(
        base_url=base_url,
        export_path=_GEOFENCES_EXPORT_PATH,
        password=password,
        username=username,
    )
    return _count_export_items(payload, keys=("geofences", "waypoints"))


def sync_participants_from_my_tracks(
    *,
    base_url: str,
    password: str,
    username: str,
) -> int:
    """Fetch participant export JSON and return the number of participants."""
    payload = _fetch_export_json(
        base_url=base_url,
        export_path=_PARTICIPANTS_EXPORT_PATH,
        password=password,
        username=username,
    )
    return _count_export_items(payload, keys=("participants", "users"))


def _count_export_items(payload: Any, *, keys: tuple[str, ...]) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    raise MyTracksSyncError(
        f"Expected export payload with list or {keys} list, got {type(payload).__name__}"
    )


def _fetch_export_json(
    *,
    base_url: str,
    export_path: str,
    password: str,
    username: str,
) -> Any:
    if username.strip() == "":
        raise MyTracksSyncError("Expected My Tracks admin username, got empty value")
    if password == "":
        raise MyTracksSyncError("Expected My Tracks admin password, got empty value")
    url = f"{base_url.rstrip('/')}{export_path}"
    try:
        response = httpx.get(
            url,
            auth=(username.strip(), password),
            timeout=_REQUEST_TIMEOUT_S,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise MyTracksSyncError(
            f"My Tracks export request failed for {url}: {exc!r}"
        ) from exc
    if response.status_code in {401, 403}:
        raise MyTracksSyncError(
            "My Tracks rejected the admin username or password"
        )
    if response.status_code == 404:
        raise MyTracksSyncError(
            f"My Tracks export endpoint not found at {export_path} — "
            "confirm the domain and My Tracks version"
        )
    if response.status_code >= 400:
        raise MyTracksSyncError(
            f"My Tracks export returned HTTP {response.status_code} for {url}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MyTracksSyncError(
            f"Expected JSON export payload from {url}, got non-JSON body"
        ) from exc
