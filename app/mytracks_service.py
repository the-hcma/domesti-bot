"""HTTP client helpers for pulling roster and geofence exports from My Tracks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.mytracks_logging import mytracks_log_host, mytracks_logger
from app.user_names import default_display_name, parse_person_name

_LOGGER = mytracks_logger(__name__)

_DOMESTI_BOT_CONFIG_PATH = "/api/admin/domesti-bot/config/"
_DOMESTI_BOT_PAIR_PATH = "/api/admin/domesti-bot/pair/"
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
class DomestiBotConfigFromMyTracks:
    domesti_base_url: str | None = None
    location_updates_enabled: bool | None = None
    user_location_test_url: str | None = None
    user_location_update_url: str | None = None


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
class ExportedUser:
    display_name: str
    enabled: bool
    first_name: str
    last_name: str
    latest_location: ExportedUserLocation | None
    tracking_device_label: str
    user_id: str


@dataclass(frozen=True)
class ExportedUserLocation:
    accuracy_m: int | None
    lat: float
    lon: float
    received_at: str


def build_location_update_webhook_urls(domesti_public_base_url: str) -> tuple[str, str]:
    """Return live and test location-update webhook URLs for a public domesti-bot origin."""
    base = normalize_public_base_url(domesti_public_base_url)
    return (
        f"{base}/v1/webhooks/location_update",
        f"{base}/v1/webhooks/location_update/test",
    )


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


def fetch_mytracks_domesti_config(
    *,
    base_url: str,
    password: str,
    username: str,
) -> DomestiBotConfigFromMyTracks:
    """Read domesti-bot integration config from my-tracks."""
    client = _login_client(base_url, username=username, password=password)
    try:
        try:
            response = client.get(_DOMESTI_BOT_CONFIG_PATH)
        except httpx.HTTPError as exc:
            raise MyTracksSyncError(
                f"My Tracks domesti-bot config request failed for {base_url}: {exc!r}"
            ) from exc
    finally:
        client.close()
    payload = _parse_export_response(
        response,
        base_url=base_url,
        export_path=_DOMESTI_BOT_CONFIG_PATH,
    )
    if not isinstance(payload, dict):
        raise MyTracksSyncError(
            f"Expected domesti-bot config object, got {type(payload).__name__}"
        )
    enabled_raw = payload.get("location_updates_enabled")
    location_updates_enabled = (
        bool(enabled_raw) if enabled_raw is not None else None
    )
    update_url = _optional_str(payload.get("user_location_update_url"))
    test_url = _optional_str(payload.get("user_location_test_url"))
    return DomestiBotConfigFromMyTracks(
        domesti_base_url=_optional_str(payload.get("domesti_base_url")),
        location_updates_enabled=location_updates_enabled,
        user_location_test_url=test_url,
        user_location_update_url=update_url,
    )


def fetch_users_from_my_tracks(
    *,
    base_url: str,
    password: str,
    username: str,
) -> list[ExportedUser]:
    """Fetch user roster export JSON from My Tracks."""
    payload = _fetch_export_json(
        base_url=base_url,
        export_path=_USERS_WITH_DEVICES_PATH,
        password=password,
        username=username,
    )
    rows = _extract_rows(payload, key="users_with_devices")
    return [_parse_user_with_device(row) for row in rows]


def normalize_public_base_url(url: str) -> str:
    """Return a canonical public HTTPS (or dev HTTP) origin for domesti-bot."""
    trimmed = url.strip().rstrip("/")
    if trimmed == "":
        raise MyTracksSyncError("Expected public base URL, got empty value")
    if not trimmed.startswith(("http://", "https://")):
        trimmed = f"https://{trimmed}"
    parsed = urlparse(trimmed)
    if parsed.netloc == "":
        raise MyTracksSyncError(f"Expected public base URL, got {url!r}")
    if parsed.scheme not in {"http", "https"}:
        raise MyTracksSyncError(f"Expected http or https public base URL, got {url!r}")
    return trimmed


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


def pair_with_my_tracks(
    *,
    api_key: str,
    base_url: str,
    domesti_base_url: str,
    user_location_test_url: str,
    user_location_update_url: str,
    password: str,
    username: str,
) -> int:
    """Register domesti-bot webhook URLs and relay secret on my-tracks.

    Returns the HTTP status code on success (typically ``200``).
    """
    if api_key.strip() == "":
        raise MyTracksSyncError("Expected relay API key, got empty value")
    _LOGGER.info(
        "pair request starting for %s as %s (domesti %s)",
        mytracks_log_host(base_url),
        username,
        mytracks_log_host(domesti_base_url),
    )
    client = _login_client(base_url, username=username, password=password)
    try:
        csrf = _session_csrf_token(client)
        try:
            response = client.post(
                _DOMESTI_BOT_PAIR_PATH,
                json={
                    "api_key": api_key,
                    "domesti_base_url": domesti_base_url,
                    "user_location_test_url": user_location_test_url,
                    "user_location_update_url": user_location_update_url,
                },
                headers={"X-CSRFToken": csrf, "Referer": f"{base_url.rstrip('/')}/"},
            )
        except httpx.HTTPError as exc:
            raise MyTracksSyncError(
                f"My Tracks pair request failed for {base_url}: {exc!r}"
            ) from exc
    finally:
        client.close()
    if response.status_code in {401, 403}:
        message = (
            "My Tracks rejected the admin session during pairing (staff account required)"
        )
        _LOGGER.warning(
            "pair request failed for %s as %s: %s",
            mytracks_log_host(base_url),
            username,
            message,
        )
        raise MyTracksSyncError(message)
    if response.status_code == 404:
        message = (
            "My Tracks domesti-bot pair endpoint not found — upgrade my-tracks to a build "
            "with /api/admin/domesti-bot/pair/"
        )
        _LOGGER.warning(
            "pair request failed for %s as %s: %s",
            mytracks_log_host(base_url),
            username,
            message,
        )
        raise MyTracksSyncError(message)
    if response.status_code >= 400:
        detail = _response_error_detail(response)
        message = f"My Tracks pair returned HTTP {response.status_code}: {detail}"
        _LOGGER.warning(
            "pair request failed for %s as %s: %s",
            mytracks_log_host(base_url),
            username,
            message,
        )
        raise MyTracksSyncError(message)
    _LOGGER.info(
        "pair request accepted for %s as %s (HTTP %d)",
        mytracks_log_host(base_url),
        username,
        response.status_code,
    )
    return response.status_code


def patch_mytracks_location_updates(
    *,
    base_url: str,
    enabled: bool,
    password: str,
    username: str,
) -> None:
    """Enable or disable live location relays on my-tracks."""
    client = _login_client(base_url, username=username, password=password)
    try:
        csrf = _session_csrf_token(client)
        try:
            response = client.patch(
                _DOMESTI_BOT_CONFIG_PATH,
                json={"location_updates_enabled": enabled},
                headers={"X-CSRFToken": csrf, "Referer": f"{base_url.rstrip('/')}/"},
            )
        except httpx.HTTPError as exc:
            raise MyTracksSyncError(
                f"My Tracks location-updates patch failed for {base_url}: {exc!r}"
            ) from exc
    finally:
        client.close()
    if response.status_code in {401, 403}:
        raise MyTracksSyncError(
            "My Tracks rejected the admin session during location-updates patch"
        )
    if response.status_code == 404:
        raise MyTracksSyncError(
            "My Tracks domesti-bot config endpoint not found — upgrade my-tracks to a build "
            "with /api/admin/domesti-bot/config/"
        )
    if response.status_code >= 400:
        detail = _response_error_detail(response)
        raise MyTracksSyncError(
            f"My Tracks location-updates patch returned HTTP {response.status_code}: {detail}"
        )


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


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed if trimmed != "" else None


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


def _parse_latest_location(row: dict[str, Any]) -> ExportedUserLocation | None:
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
    return ExportedUserLocation(
        lat=lat,
        lon=lon,
        accuracy_m=accuracy_m,
        received_at=received_at,
    )


def _parse_user_with_device(row: dict[str, Any]) -> ExportedUser:
    try:
        user_id = str(row["username"]).strip()
        export_display_name = str(row["display_name"]).strip()
        device_name = row.get("device_name", row.get("tracking_device_label"))
        tracking_device_label = str(device_name).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise MyTracksSyncError(f"Expected users-with-devices export row, got {row!r}") from exc
    if user_id == "" or export_display_name == "" or tracking_device_label == "":
        raise MyTracksSyncError(
            f"Expected non-empty users-with-devices export row, got {row!r}"
        )
    enabled_raw = row.get("enabled", True)
    enabled = bool(enabled_raw)
    first_name, last_name = parse_person_name(export_display_name)
    if first_name == "":
        first_name = user_id
    return ExportedUser(
        user_id=user_id,
        first_name=first_name,
        last_name=last_name,
        display_name=default_display_name(first_name),
        tracking_device_label=tracking_device_label,
        enabled=enabled,
        latest_location=_parse_latest_location(row),
    )


def _response_error_detail(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")
    if "json" in content_type.lower():
        try:
            payload = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error")
            if detail is not None:
                return str(detail)
    text = response.text.strip()
    return text[:200] if text else "no response body"


def _session_csrf_token(client: httpx.Client) -> str:
    csrf = client.cookies.get("csrftoken")
    if csrf is not None and csrf.strip() != "":
        return csrf
    raise MyTracksSyncError("Expected CSRF token from My Tracks session, got none")


def _resolve_csrf_token(client: httpx.Client, login_page: httpx.Response) -> str:
    csrf = client.cookies.get("csrftoken")
    if csrf is not None and csrf.strip() != "":
        return csrf
    match = _CSRF_INPUT_RE.search(login_page.text)
    if match is not None:
        return match.group(1)
    raise MyTracksSyncError("Expected CSRF token from My Tracks login page, got none")
