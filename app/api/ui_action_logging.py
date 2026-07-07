"""Structured logging for user-initiated device actions from the web UI."""

from __future__ import annotations

import logging

from starlette.requests import Request

from app.device_enums import UiActionType


def log_ui_action(
    request: Request,
    *,
    action: UiActionType,
    family_id: str | None = None,
    device_id: str | None = None,
    device_label: str | None = None,
    detail: str | None = None,
) -> None:
    """Emit one ``[ui-action]`` line per user device command."""
    client = request.client.host if request.client is not None else "?"
    verb, remaining_detail = _action_log_parts(action, detail)
    parts = [f"[ui-action] {verb}", f"client={client}"]
    if family_id is not None:
        parts.append(f"family={family_id}")
    if device_id is not None:
        parts.append(f"device={_format_device(device_id, device_label)}")
    if remaining_detail is not None:
        parts.append(remaining_detail)
    _LOGGER.info(" ".join(parts))


def _action_log_parts(
    action: UiActionType,
    detail: str | None,
) -> tuple[str, str | None]:
    if action == UiActionType.TOGGLE and detail is not None:
        if detail.startswith("on="):
            value = detail.removeprefix("on=")
            if value == "True":
                return "turn on", None
            if value == "False":
                return "turn off", None
        if detail.startswith("playing="):
            value = detail.removeprefix("playing=")
            if value == "True":
                return "resume", None
            if value == "False":
                return "pause", None
    verbs = {
        UiActionType.BULK_OFF: "turn off all",
        UiActionType.CLOSE: "close",
        UiActionType.CLOSE_ALL: "close all",
        UiActionType.OPEN: "open",
        UiActionType.PAUSE_ALL: "pause all",
        UiActionType.TOGGLE: "toggle",
    }
    return verbs[action], detail


def _format_device(device_id: str, device_label: str | None) -> str:
    if device_label is not None and device_label != device_id:
        return f"{device_label} ({device_id})"
    return device_id


_LOGGER = logging.getLogger(__name__)
