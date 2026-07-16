"""HTTP routes for operator SMTP / mail settings."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    SmtpConfigIn,
    SmtpConfigOut,
    SmtpTestEmailIn,
    SmtpTestEmailOut,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.db.secrets import SecretsConfigurationError
from app.smtp_service import SmtpConnectionParams, send_test_email, smtp_friendly_error
from app.smtp_store import (
    SmtpConfigRecord,
    SmtpConfigSave,
    delete_smtp_settings,
    load_smtp_config,
    record_smtp_test_recipient,
    resolve_password_for_send,
    save_smtp_config,
)

router = APIRouter(prefix="/v1/settings", tags=["settings"])


@router.delete("/smtp", status_code=HTTPStatus.NO_CONTENT)
async def delete_smtp_settings_route(request: Request) -> None:
    """Remove stored SMTP settings and the encrypted password."""
    cache_path = _require_discovery_cache(request)
    delete_smtp_settings(cache_path)


@router.get("/smtp", response_model=SmtpConfigOut | None)
async def get_smtp_settings(request: Request) -> SmtpConfigOut | None:
    """Return stored SMTP settings, or ``null`` when not configured."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return None
    record = load_smtp_config(cache_path)
    if record is None:
        return None
    return _to_schema(record)


@router.put("/smtp", response_model=SmtpConfigOut)
async def put_smtp_settings(body: SmtpConfigIn, request: Request) -> SmtpConfigOut:
    """Persist SMTP settings and optionally update the encrypted password."""
    cache_path = _require_discovery_cache(request)
    _validate_smtp_body(body)
    try:
        saved = save_smtp_config(
            cache_path,
            SmtpConfigSave(
                from_address=body.from_address,
                host=body.host,
                mail_domain=body.mail_domain,
                password=body.password,
                port=body.port,
                username=body.username,
            ),
        )
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return _to_schema(saved)


@router.post("/smtp/test", response_model=SmtpTestEmailOut)
async def post_smtp_test_email(body: SmtpTestEmailIn, request: Request) -> SmtpTestEmailOut:
    """Send a test email using the submitted settings (saved password may be reused)."""
    cache_path = discovery_cache_path_from_request(request)
    _validate_smtp_body(body)
    if body.to_address.strip() == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected recipient email, got empty value",
        )
    password = (
        resolve_password_for_send(
            cache_path,
            draft_password=body.password,
            host=body.host,
        )
        if cache_path is not None
        else (body.password or "")
    )
    params = SmtpConnectionParams(
        from_address=body.from_address.strip(),
        host=body.host.strip(),
        mail_domain=body.mail_domain.strip(),
        password=password,
        port=body.port,
        username=body.username.strip(),
    )
    try:
        send_test_email(
            params,
            instance_url=str(request.base_url),
            to_address=body.to_address,
        )
    except Exception as exc:
        return SmtpTestEmailOut(ok=False, message=smtp_friendly_error(exc, host=params.host))
    if cache_path is not None:
        record_smtp_test_recipient(cache_path, body.to_address)
    return SmtpTestEmailOut(
        ok=True,
        message=f"Test email sent to {body.to_address.strip()}",
    )


def _require_discovery_cache(request: Request) -> Path:
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist SMTP settings: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    return cache_path


def _to_schema(record: SmtpConfigRecord) -> SmtpConfigOut:
    return SmtpConfigOut(
        from_address=record.from_address,
        host=record.host,
        last_test_recipient=record.last_test_recipient,
        mail_domain=record.mail_domain,
        password_configured=record.password_configured,
        port=record.port,
        username=record.username,
    )


def _validate_smtp_body(body: SmtpConfigIn) -> None:
    if body.host.strip() == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected SMTP host, got empty value",
        )
    if body.mail_domain.strip() == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected mail domain, got empty value",
        )
    if body.from_address.strip() == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected from address, got empty value",
        )
