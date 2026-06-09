"""HTTP client helpers for pulling roster and geofence exports from My Tracks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

_GEOFENCES_EXPORT_PATH = "/api/admin/domesti/geofences/"
_PARTICIPANTS_EXPORT_PATH = "/api/admin/domesti/participants/"
_REQUEST_TIMEOUT_S = 30.0


class MyTracksSyncError(ValueError):
    """Raised when My Tracks export HTTP or payload parsing fails."""


@dataclass(frozen=True)
class ExportedGeofence:
    center_lat: float
    center_lon: float
    enabled: bool
    geofence_id: str
    label: str
    owntracks_rid: str | None
    radius_m: int


@dataclass(frozen=True)
class ExportedParticipant:
    display_name: str
    enabled: bool
    participant_id: str
    tracking_device_label: str


def fetch_geofences_from_my_tracks(
    *,
    base_url: str,
    password: str,
    username: str,
) -> list[ExportedGeofence]:
    """Fetch geofence export JSON from My Tracks."""
    payload = _fetch_export_json(
        base_url=base_url,
        export_path=_GEOFENCES_EXPORT_PATH,
        password=password,
        username=username,
    )
    rows = _extract_rows(payload, key="geofences")
    return [_parse_geofence(row) for row in rows]


def fetch_participants_from_my_tracks(
    *,
    base_url: str,
    password: str,
    username: str,
) -> list[ExportedParticipant]:
    """Fetch participant export JSON from My Tracks."""
    payload = _fetch_export_json(
        base_url=base_url,
        export_path=_PARTICIPANTS_EXPORT_PATH,
        password=password,
        username=username,
    )
    rows = _extract_rows(payload, key="participants")
    return [_parse_participant(row) for row in rows]


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


def _extract_rows(payload: Any, *, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    raise MyTracksSyncError(
        f"Expected export payload with {key} list, got {type(payload).__name__}"
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
    with _login_client(base_url, username=username, password=password) as client:
        url = export_path
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            raise MyTracksSyncError(
                f"My Tracks export request failed for {base_url}{export_path}: {exc!r}"
            ) from exc
    if response.status_code in {401, 403}:
        raise MyTracksSyncError(
            "My Tracks rejected the admin username or password (staff account required)"
        )
    if response.status_code == 404:
        raise MyTracksSyncError(
            f"My Tracks export endpoint not found at {export_path} — "
            "upgrade My Tracks to a build with /api/admin/domesti/* export routes"
        )
    if response.status_code >= 400:
        raise MyTracksSyncError(
            f"My Tracks export returned HTTP {response.status_code} for {base_url}{export_path}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MyTracksSyncError(
            f"Expected JSON export payload from {base_url}{export_path}, got non-JSON body"
        ) from exc


def _login_client(base_url: str, *, username: str, password: str) -> httpx.Client:
    client = httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_REQUEST_TIMEOUT_S,
        follow_redirects=False,
    )
    login_page = client.get("/login/")
    _ = login_page
    csrf = client.cookies.get("csrftoken")
    if csrf is None:
        client.close()
        raise MyTracksSyncError("Expected CSRF cookie from My Tracks login page, got none")
    response = client.post(
        "/login/",
        data={
            "username": username.strip(),
            "password": password,
            "csrfmiddlewaretoken": csrf,
        },
        headers={"Referer": f"{base_url.rstrip('/')}/login/"},
    )
    if response.status_code not in {302, 303} or client.cookies.get("sessionid") is None:
        client.close()
        raise MyTracksSyncError("My Tracks rejected the admin username or password")
    return client


def _parse_geofence(row: dict[str, Any]) -> ExportedGeofence:
    try:
        geofence_id = str(row["geofence_id"]).strip()
        label = str(row["label"]).strip()
        center_lat = float(row["center_lat"])
        center_lon = float(row["center_lon"])
        radius_m = int(row["radius_m"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MyTracksSyncError(f"Expected geofence export row, got {row!r}") from exc
    if geofence_id == "" or label == "":
        raise MyTracksSyncError(f"Expected non-empty geofence export row, got {row!r}")
    rid_raw = row.get("owntracks_rid")
    owntracks_rid = str(rid_raw).strip() if rid_raw is not None else None
    enabled_raw = row.get("enabled", True)
    enabled = bool(enabled_raw)
    return ExportedGeofence(
        geofence_id=geofence_id,
        label=label,
        center_lat=center_lat,
        center_lon=center_lon,
        radius_m=radius_m,
        enabled=enabled,
        owntracks_rid=owntracks_rid if owntracks_rid else None,
    )


def _parse_participant(row: dict[str, Any]) -> ExportedParticipant:
    try:
        participant_id = str(row["participant_id"]).strip()
        display_name = str(row["display_name"]).strip()
        tracking_device_label = str(row["tracking_device_label"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise MyTracksSyncError(f"Expected participant export row, got {row!r}") from exc
    if participant_id == "" or display_name == "" or tracking_device_label == "":
        raise MyTracksSyncError(f"Expected non-empty participant export row, got {row!r}")
    enabled_raw = row.get("enabled", True)
    enabled = bool(enabled_raw)
    return ExportedParticipant(
        participant_id=participant_id,
        display_name=display_name,
        tracking_device_label=tracking_device_label,
        enabled=enabled,
    )
