"""Scoped API keys: ``read`` < ``control`` < ``admin`` (design: ``docs/API_KEY_SCOPES.md``).

Keys come from the environment, are read on every request (like the single key before), and are
compared in constant time against every configured key before the highest matching scope is chosen,
so which scope matched is not observable through timing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.api_key_compare import api_keys_match

Scope = Literal["read", "control", "admin"]

ADMIN_KEY_ENV = "DOMESTI_ADMIN_API_KEY"
CONTROL_KEY_ENV = "DOMESTI_API_KEY"
READ_KEY_ENV = "DOMESTI_READ_API_KEY"

_LOGGER = logging.getLogger("app.api")
_RANK: dict[str, int] = {"read": 1, "control": 2, "admin": 3}
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class InsufficientScopeError(Exception):
    """A valid key whose scope is too low for the route (``403``, with ``required_scope`` in the body)."""

    def __init__(self, required_scope: Scope) -> None:
        super().__init__(f"This route needs a key with the {required_scope} scope")
        self.required_scope = required_scope


async def insufficient_scope_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`InsufficientScopeError` as ``{"detail": ..., "required_scope": ...}``."""
    if not isinstance(exc, InsufficientScopeError):
        raise exc
    return JSONResponse(
        status_code=HTTPStatus.FORBIDDEN,
        content={"detail": str(exc), "required_scope": exc.required_scope},
    )


@dataclass(frozen=True)
class ApiKeys:
    """The configured keys (stripped; blank means unset)."""

    admin: str
    control: str
    read: str

    @property
    def open_mode(self) -> bool:
        """No key at all: the API is open, exactly as before scopes existed."""
        return not (self.admin or self.control or self.read)


def configured_keys() -> ApiKeys:
    """Read the three key variables from the environment."""
    return ApiKeys(
        admin=(os.environ.get(ADMIN_KEY_ENV) or "").strip(),
        control=(os.environ.get(CONTROL_KEY_ENV) or "").strip(),
        read=(os.environ.get(READ_KEY_ENV) or "").strip(),
    )


def highest_scope(presented: str, keys: ApiKeys) -> Scope | None:
    """Highest scope the presented key grants, or ``None`` when it matches no configured key.

    Every configured key is compared (no early return). ``DOMESTI_API_KEY`` also grants ``admin``
    while ``DOMESTI_ADMIN_API_KEY`` is unset, which keeps single-key deployments unchanged.
    """
    matches_admin = bool(keys.admin) and api_keys_match(presented, keys.admin)
    matches_control = bool(keys.control) and api_keys_match(presented, keys.control)
    matches_read = bool(keys.read) and api_keys_match(presented, keys.read)
    if matches_admin or (matches_control and not keys.admin):
        return "admin"
    if matches_control:
        return "control"
    if matches_read:
        return "read"
    return None


def key_configuration_problems(keys: ApiKeys | None = None) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for the current key configuration."""
    keys = keys or configured_keys()
    errors: list[str] = []
    warnings: list[str] = []
    if keys.read and not (keys.control or keys.admin):
        errors.append(
            f"Expected {CONTROL_KEY_ENV} or {ADMIN_KEY_ENV} alongside {READ_KEY_ENV}, got only the read-only key "
            "(control and admin routes would have no key that can reach them)"
        )
    configured = [
        (name, value)
        for name, value in ((ADMIN_KEY_ENV, keys.admin), (CONTROL_KEY_ENV, keys.control), (READ_KEY_ENV, keys.read))
        if value
    ]
    for index, (name, value) in enumerate(configured):
        for other_name, other_value in configured[index + 1 :]:
            if api_keys_match(value, other_value):
                warnings.append(f"{name} and {other_name} hold the same value; the higher scope applies to it")
    if keys.control and not keys.admin:
        warnings.append(f"admin routes share the control key; set {ADMIN_KEY_ENV} to separate them")
    if keys.open_mode:
        warnings.append(
            "no API key is configured: the API is open and every route, including settings, is reachable without one"
        )
    return errors, warnings


def validate_key_configuration() -> None:
    """Fail fast on an invalid combination (called once from ``create_app``)."""
    errors, _warnings = key_configuration_problems()
    if errors:
        raise ValueError("; ".join(errors))


def log_key_configuration_warnings() -> None:
    """Log the configuration warnings once at server start (the lifespan), not on every ``create_app``."""
    _errors, warnings = key_configuration_problems()
    for warning in warnings:
        _LOGGER.warning("[api-keys] %s", warning)


def require_scope(scope: Scope) -> Callable[..., Awaitable[None]]:
    """Dependency: the request must present a key granting at least ``scope`` (or the API is open)."""

    async def _dependency(
        x_domesti_api_key: Annotated[str | None, Header(alias="X-Domesti-Api-Key")] = None,
    ) -> None:
        _check(scope, x_domesti_api_key)

    _dependency.scope_policy = (scope, scope)  # type: ignore[attr-defined]
    return _dependency


def require_scope_by_method(*, safe: Scope, unsafe: Scope) -> Callable[..., Awaitable[None]]:
    """Dependency for a router: ``GET``/``HEAD`` need ``safe``, every other method needs ``unsafe``."""

    async def _dependency(
        request: Request,
        x_domesti_api_key: Annotated[str | None, Header(alias="X-Domesti-Api-Key")] = None,
    ) -> None:
        _check(safe if request.method in _SAFE_METHODS else unsafe, x_domesti_api_key)

    _dependency.scope_policy = (safe, unsafe)  # type: ignore[attr-defined]
    return _dependency


def scope_dependency(scope: Scope) -> object:
    """``Depends(require_scope(scope))`` for route decorators."""
    return Depends(require_scope(scope))


def _check(required: Scope, presented: str | None) -> None:
    keys = configured_keys()
    if keys.open_mode:
        return
    granted = highest_scope((presented or "").strip(), keys)
    if granted is None:
        raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail="Invalid or missing X-Domesti-Api-Key")
    if _RANK[granted] < _RANK[required]:
        raise InsufficientScopeError(required)
