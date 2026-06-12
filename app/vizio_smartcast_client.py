"""Minimal async HTTPS client for Vizio SmartCast TVs (port 7345)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_VIZIO_PORT = 7345
_PAIRING_DEVICE_ID = "domesti-bot"
_PAIRING_DEVICE_NAME = "domesti-bot"
_SOURCE_HEADER = "domesti-bot"

_POWER_ON = (11, 1)
_POWER_OFF = (11, 0)


class VizioSmartCastError(Exception):
    """Base error for SmartCast transport or envelope failures."""


class VizioSmartCastAuthError(VizioSmartCastError):
    """Missing or rejected auth token."""


class VizioSmartCastConnectionError(VizioSmartCastError):
    """TCP/TLS/timeout failure talking to the TV."""


class VizioSmartCastBusyError(VizioSmartCastError):
    """Device returned BLOCKED (pairing already in progress)."""


@dataclass(frozen=True, slots=True)
class VizioDeviceInfoSnapshot:
    model_name: str
    cast_name: str
    diid: str


@dataclass(frozen=True, slots=True)
class VizioPairChallenge:
    challenge_type: int
    pairing_req_token: int


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


def device_id_for(host: str, port: int) -> str:
    """Stable UI / cache identifier for one TV endpoint."""
    if port == DEFAULT_VIZIO_PORT:
        return host
    return f"{host}:{port}"


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
        return VizioDeviceInfoSnapshot(
            model_name=model,
            cast_name=cast_name,
            diid=diid,
        )

    async def get_power_on(self) -> bool:
        payload = await self._request("GET", "/state/device/power_mode", auth=True)
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
            raise VizioSmartCastError(
                "pairing/start response missing CHALLENGE_TYPE or PAIRING_REQ_TOKEN"
            ) from exc
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
    ) -> dict[str, Any]:
        if auth and not self._auth_token:
            raise VizioSmartCastAuthError(
                f"SmartCast endpoint {path} requires an auth token for {self.device_id}"
            )
        session = await self._ensure_session()
        url = f"https://{self._host}:{self._port}{path}"
        headers = {
            "Content-Type": "application/json",
            "VIZIO-SmartCast-Source": _SOURCE_HEADER,
        }
        if auth and self._auth_token:
            headers["AUTH"] = self._auth_token
        timeout = aiohttp.ClientTimeout(total=10.0, connect=2.0)
        try:
            async with session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                ssl=False,
                timeout=timeout,
            ) as resp:
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise VizioSmartCastConnectionError(
                f"failed to reach {url}: {exc!r}"
            ) from exc
        if resp.status in (401, 403):
            raise VizioSmartCastAuthError(
                f"device returned HTTP {resp.status} for {path}"
            )
        if resp.status != 200:
            raise VizioSmartCastConnectionError(
                f"device returned HTTP {resp.status} for {path}"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VizioSmartCastError(
                f"expected JSON body from {path}, got non-JSON"
            ) from exc
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
        if result in {"REQUIRES_PAIRING", "PAIRING_DENIED"}:
            raise VizioSmartCastAuthError(detail or result)
        raise VizioSmartCastError(
            f"unexpected SmartCast status {result!r} from {path}: {detail}"
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
            self._owns_session = True
        return self._session


def _first_item_value(payload: dict[str, Any]) -> Any:
    items = payload.get("ITEMS")
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    return first.get("VALUE")
