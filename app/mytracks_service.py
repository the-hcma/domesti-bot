"""HTTP client helpers for pulling roster and geofence exports from My Tracks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

_USERS_WITH_DEVICES_PATH = "/api/admin/users-with-devices/"
_WAYPOINTS_PATH = "/api/admin/waypoints/"
_REQUEST_TIMEOUT_S = 30.0
_CSRF_INPUT_RE = re.compile(
    r'name="csrfmiddlewaretoken"\s+value="([^"]+)"',
    re.IGNORECASE,
)

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
    latest_location: ExportedParticipantLocation | None
    participant_id: str
    tracking_device_label: str


@dataclass(frozen=True)
class ExportedParticipantLocation:
    accuracy_m: int | None
    lat: float
    lon: float
    received_at: str


def fetch_geofences_from_my_tracks(
    *,
    base_url: str,
    password: str,
    username: str,
) -> list[ExportedGeofence]:
    """Fetch geofence export JSON from My Tracks."""
    payload = _fetch_export_json(
        base_url=base_url,
        export_path=_WAYPOINTS_PATH,
        password=password,
        username=username,
    )
    rows = _extract_rows(payload, key="waypoints")
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
        export_path=_USERS_WITH_DEVICES_PATH,
        password=password,
        username=username,
    )
    rows = _extract_rows(payload, key="users_with_devices")
    return [_parse_user_with_device(row) for row in rows]


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
    client = _login_client(base_url, username=username, password=password)
    try:
        try:
            response = client.get(export_path)
        except httpx.HTTPError as exc:
            raise MyTracksSyncError(
                f"My Tracks export request failed for {base_url}{export_path}: {exc!r}"
            ) from exc
    finally:
        client.close()
    return _parse_export_response(response, base_url=base_url, export_path=export_path)


def _login_client(base_url: str, *, username: str, password: str) -> httpx.Client:
    client = httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_REQUEST_TIMEOUT_S,
        follow_redirects=True,
    )
    try:
        login_page = client.get("/login/")
        if login_page.status_code >= 400:
            client.close()
            raise MyTracksSyncError(
                f"My Tracks login page returned HTTP {login_page.status_code} "
                f"for {base_url.rstrip('/')}/login/"
            )
        csrf = _resolve_csrf_token(client, login_page)
        referer = f"{base_url.rstrip('/')}/login/"
        response = client.post(
            "/login/",
            data={
                "username": username.strip(),
                "password": password,
                "csrfmiddlewaretoken": csrf,
            },
            headers={
                "Referer": referer,
                "X-CSRFToken": csrf,
            },
        )
        if client.cookies.get("sessionid") is None:
            client.close()
            if response.status_code == 403:
                raise MyTracksSyncError(
                    "My Tracks rejected the login CSRF check — verify the domain URL "
                    "matches the server"
                )
            raise MyTracksSyncError("My Tracks rejected the admin username or password")
        return client
    except MyTracksSyncError:
        raise
    except httpx.HTTPError as exc:
        client.close()
        raise MyTracksSyncError(
            f"My Tracks login request failed for {base_url.rstrip('/')}/login/: {exc!r}"
        ) from exc
    except Exception:
        client.close()
        raise


def _parse_export_response(
    response: httpx.Response,
    *,
    base_url: str,
    export_path: str,
) -> Any:
    if response.status_code in {401, 403}:
        raise MyTracksSyncError(
            "My Tracks rejected the admin session (staff account required)"
        )
    if response.status_code == 404:
        raise MyTracksSyncError(
            f"My Tracks export endpoint not found at {export_path} — "
            "upgrade My Tracks to a build with /api/admin/users-with-devices/ "
            "and /api/admin/waypoints/ export routes"
        )
    if response.status_code >= 400:
        raise MyTracksSyncError(
            f"My Tracks export returned HTTP {response.status_code} for {base_url}{export_path}"
        )
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise MyTracksSyncError(
            f"My Tracks export returned non-JSON from {base_url}{export_path} "
            f"(content-type {content_type!r}) — check the domain URL and admin credentials"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MyTracksSyncError(
            f"Expected JSON export payload from {base_url}{export_path}, got non-JSON body"
        ) from exc


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


def _parse_latest_location(row: dict[str, Any]) -> ExportedParticipantLocation | None:
    raw = row.get("latest_location")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MyTracksSyncError(
            f"Expected latest_location object in users-with-devices row, got {raw!r}"
        )
    try:
        lat = float(raw["lat"])
        lon = float(raw["lon"])
        received_at = str(raw["timestamp"]).strip()
        accuracy_raw = raw.get("accuracy_m")
        accuracy_m = int(accuracy_raw) if accuracy_raw is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise MyTracksSyncError(
            f"Expected latest_location export object, got {raw!r}"
        ) from exc
    if received_at == "":
        raise MyTracksSyncError(
            f"Expected non-empty latest_location.timestamp, got {raw!r}"
        )
    return ExportedParticipantLocation(
        lat=lat,
        lon=lon,
        accuracy_m=accuracy_m,
        received_at=received_at,
    )


def _parse_user_with_device(row: dict[str, Any]) -> ExportedParticipant:
    try:
        participant_id = str(row["username"]).strip()
        display_name = str(row["display_name"]).strip()
        device_name = row.get("device_name", row.get("tracking_device_label"))
        tracking_device_label = str(device_name).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise MyTracksSyncError(f"Expected users-with-devices export row, got {row!r}") from exc
    if participant_id == "" or display_name == "" or tracking_device_label == "":
        raise MyTracksSyncError(
            f"Expected non-empty users-with-devices export row, got {row!r}"
        )
    enabled_raw = row.get("enabled", True)
    enabled = bool(enabled_raw)
    return ExportedParticipant(
        participant_id=participant_id,
        display_name=display_name,
        tracking_device_label=tracking_device_label,
        enabled=enabled,
        latest_location=_parse_latest_location(row),
    )


def _resolve_csrf_token(client: httpx.Client, login_page: httpx.Response) -> str:
    csrf = client.cookies.get("csrftoken")
    if csrf is not None and csrf.strip() != "":
        return csrf
    match = _CSRF_INPUT_RE.search(login_page.text)
    if match is not None:
        return match.group(1)
    raise MyTracksSyncError("Expected CSRF token from My Tracks login page, got none")
