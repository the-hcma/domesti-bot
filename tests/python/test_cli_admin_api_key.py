"""Remote REPL and the admin API key: ``POST /v1/execute-line`` needs the admin scope."""

from __future__ import annotations

import httpx
import pytest

from app.domesti_bot_cli import (
    _remote_api_key_headers,
    _remote_execute_line_hint,
    _remote_key_headers,
    build_arg_parser,
)


def test_admin_api_key_flag_and_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVICE_MANAGER_ADMIN_API_KEY", raising=False)
    assert build_arg_parser().parse_args([]).admin_api_key is None
    assert build_arg_parser().parse_args(["--admin-api-key", "adm"]).admin_api_key == "adm"
    monkeypatch.setenv("DEVICE_MANAGER_ADMIN_API_KEY", "  from-env  ")
    assert build_arg_parser().parse_args([]).admin_api_key == "from-env"
    assert build_arg_parser().parse_args(["--admin-api-key", "flag"]).admin_api_key == "flag"


def test_headers_strip_and_omit_blank_keys() -> None:
    assert _remote_api_key_headers(" k ") == {"X-Domesti-Api-Key": "k"}
    assert _remote_api_key_headers("") == {}
    assert _remote_api_key_headers(None) == {}


def test_a_forbidden_execute_line_tells_the_user_which_key_to_pass() -> None:
    hint = _remote_execute_line_hint(httpx.codes.FORBIDDEN, has_admin_key=False)
    assert hint is not None
    assert "--admin-api-key" in hint and "DEVICE_MANAGER_ADMIN_API_KEY" in hint


def test_a_forbidden_execute_line_with_an_admin_key_points_at_the_server_config() -> None:
    hint = _remote_execute_line_hint(httpx.codes.FORBIDDEN, has_admin_key=True)
    assert hint is not None
    assert "DOMESTI_ADMIN_API_KEY" in hint


def test_an_unauthorized_execute_line_says_the_key_was_not_accepted() -> None:
    hint = _remote_execute_line_hint(httpx.codes.UNAUTHORIZED, has_admin_key=False)
    assert hint is not None
    assert "not accepted" in hint


@pytest.mark.parametrize("status", [200, 404, 500, 503])
def test_other_statuses_get_no_extra_hint(status: int) -> None:
    assert _remote_execute_line_hint(status, has_admin_key=True) is None


@pytest.mark.parametrize(
    ("api_key", "admin_api_key", "reads", "execute"),
    [
        ("ctl", "adm", "ctl", "adm"),
        ("ctl", None, "ctl", "ctl"),
        (None, "adm", "adm", "adm"),
        (None, None, None, None),
        ("  ctl  ", "  adm  ", "ctl", "adm"),
        ("ctl", "   ", "ctl", "ctl"),
        ("   ", "adm", "adm", "adm"),
    ],
)
def test_reads_use_the_regular_key_and_execute_line_prefers_the_admin_key(
    api_key: str | None, admin_api_key: str | None, reads: str | None, execute: str | None
) -> None:
    read_headers, execute_headers = _remote_key_headers(api_key, admin_api_key)
    assert read_headers.get("X-Domesti-Api-Key") == reads
    assert execute_headers.get("X-Domesti-Api-Key") == execute


@pytest.mark.asyncio
async def test_the_per_request_admin_header_overrides_the_client_default_on_the_wire() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("X-Domesti-Api-Key")))
        return httpx.Response(200, json={})

    read_headers, execute_headers = _remote_key_headers("ctl", "adm")
    async with httpx.AsyncClient(
        base_url="http://test", headers=read_headers, transport=httpx.MockTransport(handler)
    ) as client:
        await client.get("/v1/completion-aliases")
        await client.post("/v1/execute-line", json={"line": "help"}, headers=execute_headers)

    assert seen == [("/v1/completion-aliases", "ctl"), ("/v1/execute-line", "adm")]
