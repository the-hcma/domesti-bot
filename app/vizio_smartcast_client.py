"""Minimal async HTTPS client for Vizio SmartCast TVs (port 7345)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.vizio_mac import lookup_mac_via_arp, normalize_mac

_LOGGER = logging.getLogger(__name__)

DEFAULT_VIZIO_PORT = 7345
_PAIRING_DEVICE_ID = "domesti-bot"
_PAIRING_DEVICE_NAME = "domesti-bot"
_SOURCE_HEADER = "domesti-bot"

_POWER_ON = (11, 1)
_POWER_OFF = (11, 0)

_DEFAULT_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10.0, connect=2.0)
_POLL_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=3.0, connect=1.0)

_MAC_KEY_RE = re.compile(r"mac", re.I)
_MAC_COLON_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_MAC_PLAIN_RE = re.compile(r"^[0-9a-fA-F]{12}$")


class VizioSmartCastError(Exception):
    """Base error for SmartCast transport or envelope failures."""


class VizioSmartCastAuthError(VizioSmartCastError):
    """Missing or rejected auth token."""


class VizioSmartCastConnectionError(VizioSmartCastError):
    """TCP/TLS/timeout failure talking to the TV."""


class VizioSmartCastBusyError(VizioSmartCastError):
    """Device returned BLOCKED (pairing already in progress)."""


class VizioSmartCastNotFoundError(VizioSmartCastError):
    """Firmware does not expose the requested SmartCast endpoint."""


@dataclass(frozen=True, slots=True)
class VizioDeviceInfoSnapshot:
    model_name: str
    cast_name: str
    diid: str
    mac: str | None = None


@dataclass(frozen=True, slots=True)
class VizioPairChallenge:
    challenge_type: int
    pairing_req_token: int


@dataclass(frozen=True, slots=True)
class VizioStateExtendedSnapshot:
    """Aggregate TV state from ``GET /state_extended`` (modern firmware)."""

    power_on: bool
    power_mode: str
    current_input: str
    media_state: str
    has_current_app: bool


def coerce_mac_value(value: Any) -> str | None:
    """Normalize a SmartCast MAC field value, or return ``None``."""
    if value is None:
        return None
    if isinstance(value, int):
        value = f"{value:012x}"
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if _MAC_COLON_RE.match(text) or _MAC_PLAIN_RE.match(text):
            return normalize_mac(text)
    except ValueError:
        return None
    return None


def device_id_for(host: str, port: int) -> str:
    """Stable UI / cache identifier for one TV endpoint."""
    if port == DEFAULT_VIZIO_PORT:
        return host
    return f"{host}:{port}"


def extract_mac_from_payload(obj: Any) -> str | None:
    """Walk a SmartCast JSON payload and return the first plausible MAC."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and _MAC_KEY_RE.search(key):
                mac = coerce_mac_value(value)
                if mac is not None:
                    return mac
            mac = extract_mac_from_payload(value)
            if mac is not None:
                return mac
        return None
    if isinstance(obj, list):
        for item in obj:
            mac = extract_mac_from_payload(item)
            if mac is not None:
                return mac
        return None
    return coerce_mac_value(obj)


def parse_host_spec(raw: str, *, default_port: int = DEFAULT_VIZIO_PORT) -> tuple[str, int]:
    """Parse ``HOST`` or ``HOST:PORT`` into ``(host, port)``."""
    text = raw.strip()
    if not text:
        raise ValueError("Expected a non-empty host spec, got whitespace only")
    if ":" in text:
        host, port_s = text.rsplit(":", 1)
        host = host.strip()
        if not host:
            raise ValueError(f"Expected a host before ':', got {raw!r}")
        try:
            port = int(port_s)
        except ValueError as exc:
            raise ValueError(f"Expected an integer port in {raw!r}") from exc
        if port < 1 or port > 65535:
            raise ValueError(f"Expected port in 1..65535, got {port}")
        return host, port
    return text, default_port


def parse_state_extended(payload: dict[str, Any]) -> VizioStateExtendedSnapshot:
    """Parse the flat ``/state_extended`` JSON envelope."""
    ci_payload = _case_insensitive_mapping(payload)

    power_on = False
    power_status = ci_payload.get("power_status")
    if isinstance(power_status, dict):
        power_on = bool(_case_insensitive_mapping(power_status).get("value", 0))

    power_mode = ""
    power_mode_raw = ci_payload.get("power_mode")
    if isinstance(power_mode_raw, dict):
        power_mode = _optional_string(_case_insensitive_mapping(power_mode_raw).get("value"))

    current_input = ""
    current_input_raw = ci_payload.get("current_input")
    if isinstance(current_input_raw, dict):
        current_input = _optional_string(_case_insensitive_mapping(current_input_raw).get("name"))

    has_current_app = False
    app_current_raw = ci_payload.get("app_current")
    if isinstance(app_current_raw, dict):
        app_current = _case_insensitive_mapping(app_current_raw)
        has_current_app = app_current.get("app_id") is not None

    return VizioStateExtendedSnapshot(
        power_on=power_on,
        power_mode=power_mode,
        current_input=current_input,
        media_state=_optional_string(ci_payload.get("media_state")),
        has_current_app=has_current_app,
    )


async def resolve_vizio_tv_mac(
    client: VizioSmartCastClient,
    *,
    host: str,
) -> str | None:
    """Return a normalized MAC from SmartCast network info, else local ARP."""
    try:
        mac = await client.fetch_network_mac()
        if mac is not None:
            return mac
    except (
        VizioSmartCastAuthError,
        VizioSmartCastConnectionError,
        VizioSmartCastNotFoundError,
    ) as exc:
        _LOGGER.debug("SmartCast network MAC lookup for %s failed: %s", host, exc)
    return await asyncio.to_thread(lookup_mac_via_arp, host)


def tv_is_active(*, power_on: bool, media_state: str = "") -> bool:
    """Return whether the TV should read as on in the domesti-bot UI."""
    if power_on:
        return True
    media_label = media_state.rsplit("::", 1)[-1].strip().lower()
    return media_label == "playing"


class VizioSmartCastClient:
    """One TV endpoint. Caller owns session lifetime when ``session`` is passed in."""

    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_VIZIO_PORT,
        auth_token: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._host = host.strip()
        self._port = port
        self._auth_token = (auth_token or "").strip() or None
        self._session = session
        self._owns_session = session is None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def device_id(self) -> str:
        return device_id_for(self._host, self._port)

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch_deviceinfo(self) -> VizioDeviceInfoSnapshot:
        payload = await self._request("GET", "/state/device/deviceinfo", auth=False)
        value = _first_item_value(payload) or {}
        if not isinstance(value, dict):
            value = {}
        system = value.get("SYSTEM_INFO")
        diid = ""
        if isinstance(system, dict):
            raw_diid = system.get("DIID")
            if isinstance(raw_diid, str):
                diid = raw_diid.strip()
        model = str(value.get("MODEL_NAME") or "").strip()
        cast_name = str(value.get("CAST_NAME") or "").strip()
        mac = extract_mac_from_payload(value)
        return VizioDeviceInfoSnapshot(
            model_name=model,
            cast_name=cast_name,
            diid=diid,
            mac=mac,
        )

    async def fetch_network_mac(self) -> str | None:
        """Read the TV MAC from authenticated network settings endpoints."""
        for path in (
            "/state/network/networkinfo",
            "/menu_native/dynamic/tv_settings/network",
        ):
            try:
                payload = await self._request("GET", path, auth=True)
            except (VizioSmartCastAuthError, VizioSmartCastConnectionError):
                continue
            mac = extract_mac_from_payload(payload)
            if mac is not None:
                return mac
        return None

    async def fetch_state_extended(
        self,
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> VizioStateExtendedSnapshot:
        """Bulk power/input/app/media poll for firmware with ``state_extended``."""
        payload = await self._request_raw_json(
            "/state_extended",
            auth=True,
            timeout=timeout,
        )
        return parse_state_extended(payload)

    async def fetch_tv_active_state(self, *, poll: bool = False) -> bool:
        """Return whether the TV is on or actively playing media (e.g. Cast)."""
        timeout = _POLL_REQUEST_TIMEOUT if poll else _DEFAULT_REQUEST_TIMEOUT
        try:
            snapshot = await self.fetch_state_extended(timeout=timeout)
        except VizioSmartCastNotFoundError:
            return await self.get_power_on(timeout=timeout)
        return tv_is_active(
            power_on=snapshot.power_on,
            media_state=snapshot.media_state,
        )

    async def get_power_on(
        self,
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> bool:
        payload = await self._request(
            "GET",
            "/state/device/power_mode",
            auth=True,
            timeout=timeout,
        )
        value = _first_item_value(payload)
        return bool(value)

    async def power_on(self) -> None:
        await self._send_key(_POWER_ON)

    async def power_off(self) -> None:
        await self._send_key(_POWER_OFF)

    async def pair_begin(
        self,
        *,
        device_id: str = _PAIRING_DEVICE_ID,
        device_name: str = _PAIRING_DEVICE_NAME,
    ) -> VizioPairChallenge:
        body = {"DEVICE_ID": device_id, "DEVICE_NAME": device_name}
        payload = await self._request("PUT", "/pairing/start", json_body=body, auth=False)
        item = payload.get("ITEM")
        if not isinstance(item, dict):
            raise VizioSmartCastError("pairing/start response missing ITEM")
        try:
            challenge_type = int(item["CHALLENGE_TYPE"])
            pairing_req_token = int(item["PAIRING_REQ_TOKEN"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VizioSmartCastError("pairing/start response missing CHALLENGE_TYPE or PAIRING_REQ_TOKEN") from exc
        return VizioPairChallenge(
            challenge_type=challenge_type,
            pairing_req_token=pairing_req_token,
        )

    async def pair_complete(
        self,
        *,
        challenge: VizioPairChallenge,
        pin: str,
        device_id: str = _PAIRING_DEVICE_ID,
    ) -> str:
        body = {
            "DEVICE_ID": device_id,
            "CHALLENGE_TYPE": challenge.challenge_type,
            "PAIRING_REQ_TOKEN": challenge.pairing_req_token,
            "RESPONSE_VALUE": pin.strip(),
        }
        payload = await self._request("PUT", "/pairing/pair", json_body=body, auth=False)
        item = payload.get("ITEM")
        if not isinstance(item, dict):
            raise VizioSmartCastError("pairing/pair response missing ITEM")
        token = item.get("AUTH_TOKEN")
        if not isinstance(token, str) or not token.strip():
            raise VizioSmartCastError("pairing/pair response missing AUTH_TOKEN")
        return token.strip()

    async def pair_cancel(
        self,
        *,
        challenge: VizioPairChallenge,
        device_id: str = _PAIRING_DEVICE_ID,
    ) -> None:
        body = {
            "DEVICE_ID": device_id,
            "CHALLENGE_TYPE": challenge.challenge_type,
            "RESPONSE_VALUE": "1111",
            "PAIRING_REQ_TOKEN": challenge.pairing_req_token,
        }
        await self._request("PUT", "/pairing/cancel", json_body=body, auth=False)

    async def _send_key(self, codeset_code: tuple[int, int]) -> None:
        codeset, code = codeset_code
        body = {
            "KEYLIST": [{"CODESET": codeset, "CODE": code, "ACTION": "KEYPRESS"}],
        }
        await self._request("PUT", "/key_command/", json_body=body, auth=True)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        auth: bool,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any]:
        if auth and not self._auth_token:
            raise VizioSmartCastAuthError(f"SmartCast endpoint {path} requires an auth token for {self.device_id}")
        session = await self._ensure_session()
        url = f"https://{self._host}:{self._port}{path}"
        headers = {
            "Content-Type": "application/json",
            "VIZIO-SmartCast-Source": _SOURCE_HEADER,
        }
        if auth and self._auth_token:
            headers["AUTH"] = self._auth_token
        request_timeout = timeout or _DEFAULT_REQUEST_TIMEOUT
        try:
            async with session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                ssl=False,
                timeout=request_timeout,
            ) as resp:
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise VizioSmartCastConnectionError(f"failed to reach {url}: {exc!r}") from exc
        if resp.status in (401, 403):
            raise VizioSmartCastAuthError(f"device returned HTTP {resp.status} for {path}")
        if resp.status != 200:
            raise VizioSmartCastConnectionError(f"device returned HTTP {resp.status} for {path}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VizioSmartCastError(f"expected JSON body from {path}, got non-JSON") from exc
        if not isinstance(payload, dict):
            raise VizioSmartCastError(f"expected JSON object from {path}")
        status = payload.get("STATUS")
        if not isinstance(status, dict):
            raise VizioSmartCastError(f"response from {path} missing STATUS object")
        result = str(status.get("RESULT") or "").upper()
        detail = str(status.get("DETAIL") or "")
        if result == "SUCCESS":
            return payload
        if result == "BLOCKED":
            raise VizioSmartCastBusyError(detail or "Operation blocked")
        if result == "URI_NOT_FOUND":
            raise VizioSmartCastNotFoundError(detail or f"device has no endpoint at {path}")
        if result in {"REQUIRES_PAIRING", "PAIRING_DENIED"}:
            raise VizioSmartCastAuthError(detail or result)
        raise VizioSmartCastError(f"unexpected SmartCast status {result!r} from {path}: {detail}")

    async def _request_raw_json(
        self,
        path: str,
        *,
        auth: bool,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any]:
        """Issue GET and return parsed JSON without requiring the SCPL ITEMS envelope."""
        if auth and not self._auth_token:
            raise VizioSmartCastAuthError(f"SmartCast endpoint {path} requires an auth token for {self.device_id}")
        session = await self._ensure_session()
        url = f"https://{self._host}:{self._port}{path}"
        headers = {
            "Content-Type": "application/json",
            "VIZIO-SmartCast-Source": _SOURCE_HEADER,
        }
        if auth and self._auth_token:
            headers["AUTH"] = self._auth_token
        request_timeout = timeout or _DEFAULT_REQUEST_TIMEOUT
        try:
            async with session.request(
                "GET",
                url,
                headers=headers,
                ssl=False,
                timeout=request_timeout,
            ) as resp:
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise VizioSmartCastConnectionError(f"failed to reach {url}: {exc!r}") from exc
        if resp.status in (401, 403):
            raise VizioSmartCastAuthError(f"device returned HTTP {resp.status} for {path}")
        if resp.status == 404:
            raise VizioSmartCastNotFoundError(f"device has no endpoint at {path}")
        if resp.status != 200:
            raise VizioSmartCastConnectionError(f"device returned HTTP {resp.status} for {path}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VizioSmartCastError(f"expected JSON body from {path}, got non-JSON") from exc
        if not isinstance(payload, dict):
            raise VizioSmartCastError(f"expected JSON object from {path}")
        status = payload.get("STATUS")
        if isinstance(status, dict):
            result = str(status.get("RESULT") or "").upper()
            detail = str(status.get("DETAIL") or "")
            if result == "URI_NOT_FOUND":
                raise VizioSmartCastNotFoundError(detail or f"device has no endpoint at {path}")
            if result not in {"", "SUCCESS"}:
                if result in {"REQUIRES_PAIRING", "PAIRING_DENIED"}:
                    raise VizioSmartCastAuthError(detail or result)
                raise VizioSmartCastError(f"unexpected SmartCast status {result!r} from {path}: {detail}")
        return payload

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
            self._owns_session = True
        return self._session


def _case_insensitive_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {str(key).lower(): value for key, value in mapping.items()}


def _first_item_value(payload: dict[str, Any]) -> Any:
    items = payload.get("ITEMS")
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    return first.get("VALUE")


def _optional_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""
