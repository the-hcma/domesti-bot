"""my-tracks location-request rate limits cached from admin config / pair."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LocationRequestRateLimits:
    device_cooldown_seconds: int
    user_cooldown_seconds: int
    user_cooldown_seconds_by_reason: dict[str, int] | None = None

    def effective_user_cooldown_seconds(self, reason: str) -> int:
        by_reason = self.user_cooldown_seconds_by_reason
        if by_reason is not None:
            tiered = by_reason.get(reason)
            if tiered is not None:
                return tiered
        return self.user_cooldown_seconds


def _parse_rate_limits_dict(raw_limits: dict[str, Any]) -> LocationRequestRateLimits | None:
    user_raw = raw_limits.get("user_cooldown_seconds")
    device_raw = raw_limits.get("device_cooldown_seconds")
    if (
        type(user_raw) is not int
        or type(device_raw) is not int
        or user_raw < 0
        or device_raw < 0
    ):
        return None
    by_reason_raw = raw_limits.get("user_cooldown_seconds_by_reason")
    by_reason: dict[str, int] | None = None
    if isinstance(by_reason_raw, dict):
        parsed: dict[str, int] = {}
        for key, value in by_reason_raw.items():
            if isinstance(key, str) and type(value) is int and value >= 0:
                parsed[key] = value
        by_reason = parsed or None
    return LocationRequestRateLimits(
        device_cooldown_seconds=device_raw,
        user_cooldown_seconds=user_raw,
        user_cooldown_seconds_by_reason=by_reason,
    )


def _rate_limits_from_flat_payload(payload: dict[str, Any]) -> LocationRequestRateLimits | None:
    user_raw = payload.get("location_request_user_cooldown_seconds")
    device_raw = payload.get("location_request_device_cooldown_seconds")
    if (
        type(user_raw) is int
        and type(device_raw) is int
        and user_raw >= 0
        and device_raw >= 0
    ):
        return LocationRequestRateLimits(
            device_cooldown_seconds=device_raw,
            user_cooldown_seconds=user_raw,
            user_cooldown_seconds_by_reason=None,
        )
    return None


def location_request_rate_limits_from_payload(
    payload: dict[str, Any] | None,
) -> LocationRequestRateLimits | None:
    if payload is None:
        return None
    raw_limits = payload.get("location_request_rate_limits")
    if not isinstance(raw_limits, dict):
        return _rate_limits_from_flat_payload(payload)
    return _parse_rate_limits_dict(raw_limits)


def serialize_user_cooldown_by_reason(
    value: dict[str, int] | None,
) -> str | None:
    if value is None or not value:
        return None
    return json.dumps(value, sort_keys=True)


def user_cooldown_by_reason_from_json(raw: str | None) -> dict[str, int] | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    result: dict[str, int] = {}
    for key, item in parsed.items():
        if isinstance(key, str) and type(item) is int and item >= 0:
            result[key] = item
    return result or None
