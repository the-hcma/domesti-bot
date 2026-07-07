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
    detail: str | None = None,
) -> None:
    """Emit one ``[ui-action]`` line per user device command."""
    client = request.client.host if request.client is not None else "?"
    parts = [f"[ui-action] {action}", f"client={client}"]
    if family_id is not None:
        parts.append(f"family={family_id}")
    if device_id is not None:
        parts.append(f"device={device_id}")
    if detail is not None:
        parts.append(detail)
    _LOGGER.info(" ".join(parts))


_LOGGER = logging.getLogger(__name__)
