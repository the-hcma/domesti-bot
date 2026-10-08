"""Scoped API keys (``docs/API_KEY_SCOPES.md``): the route inventory, the key matrix and the startup rules."""

from __future__ import annotations

import argparse
import re
import shutil
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.api_scopes import ApiKeys, highest_scope, key_configuration_problems
from app.api.app import create_app

_ADMIN, _CONTROL, _READ = "adm-key-1", "ctl-key-2", "rd-key-3"
_RANK = {"read": 1, "control": 2, "admin": 3}
_PUBLIC = "public"
_RELAY = "relay"


def _expected_scope(method: str, path: str) -> str:
    """The design table, written independently of the app code."""
    if path.startswith("/v1/webhooks/"):
        return _RELAY
    if path in ("/health", "/v1/meta"):
        return _PUBLIC
    if path.startswith("/v1/settings/") or path == "/v1/execute-line":
        return "admin"
    if method == "POST" and path in ("/v1/rules/geofences/sync", "/v1/rules/users/sync"):
        return "admin"
    if path in ("/v1/ui/state", "/v1/completion-aliases"):
        return "read"
    if path.startswith(("/v1/rules", "/v1/sensor-collection")) and method == "GET":
        return "read"
    if path.startswith(("/v1/ui/", "/v1/location_update/", "/v1/rules", "/v1/sensor-collection")):
        return "control"
    raise AssertionError(f"Unclassified route {method} {path}: add it to _expected_scope and docs/API_KEY_SCOPES.md")


def _app(tmp_path: Path) -> TestClient:
    args = argparse.Namespace(discovery_cache=str(tmp_path / "ui.sqlite"), tailwind_token=None)
    return TestClient(create_app(args), raise_server_exceptions=False)


def _routes(client: TestClient) -> list[tuple[str, str]]:
    spec = client.app.openapi()  # type: ignore[attr-defined]
    return sorted(
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method in item
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    )


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _send(client: TestClient, method: str, path: str, key: str | None) -> tuple[int, dict[str, object]]:
    headers = {"X-Domesti-Api-Key": key} if key is not None else {}
    kwargs: dict[str, object] = {"headers": headers}
    if method in {"POST", "PUT", "PATCH"}:
        # Valid JSON, so FastAPI reaches the auth dependency (malformed JSON is rejected before it runs).
        kwargs["content"] = b"{}"
        headers["Content-Type"] = "application/json"
    response = client.request(method, _concrete(path), **kwargs)  # type: ignore[arg-type]
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The route tests send ``{}`` to every write route; keep the rules writes off the committed example bundle.
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    bundle = tmp_path / "automation-rules.json"
    shutil.copy(example, bundle)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    for name in ("DOMESTI_API_KEY", "DOMESTI_ADMIN_API_KEY", "DOMESTI_READ_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_ADMIN_API_KEY", _ADMIN)
    monkeypatch.setenv("DOMESTI_API_KEY", _CONTROL)
    monkeypatch.setenv("DOMESTI_READ_API_KEY", _READ)


def test_inventory_matches_the_design_table_and_has_no_unclassified_route(tmp_path: Path) -> None:
    client = _app(tmp_path)
    routes = _routes(client)
    assert len(routes) >= 78
    classes = {_expected_scope(m, p) for m, p in routes}
    assert classes <= {"read", "control", "admin", _PUBLIC, _RELAY}
    assert {p for m, p in routes if _expected_scope(m, p) == "admin" and p.startswith("/v1/rules")} == {
        "/v1/rules/geofences/sync",
        "/v1/rules/users/sync",
    }


def test_every_route_enforces_exactly_its_scope_for_every_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _all_keys(monkeypatch)
    client = _app(tmp_path)
    keys = {"read": _READ, "control": _CONTROL, "admin": _ADMIN}
    failures: list[str] = []
    for method, path in _routes(client):
        required = _expected_scope(method, path)
        if required == _RELAY:
            continue
        for label, key in (("none", None), ("wrong", "nope"), *keys.items()):
            status, body = _send(client, method, path, key)
            if required == _PUBLIC:
                ok = status not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
            elif label in ("none", "wrong"):
                ok = status == HTTPStatus.UNAUTHORIZED
            elif _RANK[label] >= _RANK[required]:
                ok = status not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
            else:
                ok = status == HTTPStatus.FORBIDDEN and body.get("required_scope") == required
            if not ok:
                failures.append(f"{method} {path} needs {required}, key={label}: got {status} {body}")
    assert failures == []


def test_a_key_that_is_too_weak_is_403_but_a_mistyped_admin_key_is_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_keys(monkeypatch)
    client = _app(tmp_path)
    assert _send(client, "GET", "/v1/settings/smtp", _CONTROL)[0] == HTTPStatus.FORBIDDEN
    assert _send(client, "GET", "/v1/settings/smtp", _ADMIN + "x")[0] == HTTPStatus.UNAUTHORIZED
    status, body = _send(client, "GET", "/v1/settings/smtp", _READ)
    assert status == HTTPStatus.FORBIDDEN
    assert set(body) == {"detail", "required_scope"}
    assert body["required_scope"] == "admin"
    assert _ADMIN not in str(body) and _CONTROL not in str(body)


def test_single_key_deployments_are_unchanged_including_settings_and_execute_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOMESTI_API_KEY", _CONTROL)
    client = _app(tmp_path)
    for method, path in _routes(client):
        required = _expected_scope(method, path)
        if required == _RELAY:
            continue
        none_status, _ = _send(client, method, path, None)
        key_status, _ = _send(client, method, path, _CONTROL)
        if required == _PUBLIC:
            assert none_status != HTTPStatus.UNAUTHORIZED, (method, path)
        else:
            assert none_status == HTTPStatus.UNAUTHORIZED, (method, path)
            assert key_status not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN), (method, path)


def test_open_mode_needs_no_key_anywhere(tmp_path: Path) -> None:
    client = _app(tmp_path)
    for method, path in _routes(client):
        if _expected_scope(method, path) == _RELAY:
            continue
        status, _ = _send(client, method, path, None)
        assert status not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN), (method, path)


def test_an_admin_key_alone_is_fail_closed_and_covers_every_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOMESTI_ADMIN_API_KEY", _ADMIN)
    client = _app(tmp_path)
    assert _send(client, "GET", "/v1/ui/state", None)[0] == HTTPStatus.UNAUTHORIZED
    assert _send(client, "POST", "/v1/ui/global/bulk-off", None)[0] == HTTPStatus.UNAUTHORIZED
    assert _send(client, "GET", "/v1/ui/state", _ADMIN)[0] not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
    assert _send(client, "GET", "/v1/settings/smtp", _ADMIN)[0] not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)


def test_a_read_key_without_a_control_or_admin_key_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOMESTI_READ_API_KEY", _READ)
    with pytest.raises(ValueError, match="alongside DOMESTI_READ_API_KEY"):
        _app(tmp_path)


def test_webhooks_are_not_governed_by_the_scope_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _all_keys(monkeypatch)
    client = _app(tmp_path)
    for key in (_ADMIN, _CONTROL, _READ):
        status, _ = _send(client, "POST", "/v1/webhooks/location_update", key)
        assert status in (
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
            HTTPStatus.SERVICE_UNAVAILABLE,
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )


def test_preflight_and_public_paths_need_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _all_keys(monkeypatch)
    monkeypatch.setenv("DOMESTI_CORS_ORIGINS", "https://ui.example.test")
    client = _app(tmp_path)
    assert client.get("/health").status_code == HTTPStatus.OK
    assert client.get("/v1/meta").status_code == HTTPStatus.OK
    assert client.get("/openapi.json").status_code == HTTPStatus.OK
    for path in ("/", "/sw.js", "/favicon.ico", "/docs", "/redoc", "/static/index.html"):
        assert client.get(path).status_code not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN), path
    preflight = client.options(
        "/v1/settings/smtp",
        headers={"Origin": "https://ui.example.test", "Access-Control-Request-Method": "PUT"},
    )
    assert preflight.status_code == HTTPStatus.OK


@pytest.mark.parametrize(
    ("presented", "expected"),
    [
        (_ADMIN, "admin"),
        (_CONTROL, "control"),
        (_READ, "read"),
        ("", None),
        ("nope", None),
        ("clé-secrète", None),
        ("x" * 5000, None),
    ],
)
def test_highest_scope_for_every_kind_of_presented_key(presented: str, expected: str | None) -> None:
    keys = ApiKeys(admin=_ADMIN, control=_CONTROL, read=_READ)
    assert highest_scope(presented, keys) == expected


def test_the_control_key_grants_admin_only_while_no_admin_key_is_set() -> None:
    assert highest_scope(_CONTROL, ApiKeys(admin="", control=_CONTROL, read="")) == "admin"
    assert highest_scope(_CONTROL, ApiKeys(admin=_ADMIN, control=_CONTROL, read="")) == "control"


def test_equal_values_are_allowed_with_a_warning_and_the_higher_scope_applies() -> None:
    keys = ApiKeys(admin="same", control="same", read="")
    errors, warnings = key_configuration_problems(keys)
    assert errors == []
    assert any("same value" in w for w in warnings)
    assert highest_scope("same", keys) == "admin"


def test_blank_values_count_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_API_KEY", "   ")
    monkeypatch.setenv("DOMESTI_ADMIN_API_KEY", "")
    client = _app(tmp_path)
    assert _send(client, "GET", "/v1/settings/smtp", None)[0] != HTTPStatus.UNAUTHORIZED


def test_startup_warnings_describe_the_mode() -> None:
    _errors, open_mode = key_configuration_problems(ApiKeys(admin="", control="", read=""))
    assert any("API is open" in w for w in open_mode)
    assert not any("share the control key" in w for w in open_mode)

    _errors, shared = key_configuration_problems(ApiKeys(admin="", control=_CONTROL, read=""))
    assert any("share the control key" in w for w in shared)

    _errors, separate = key_configuration_problems(ApiKeys(admin=_ADMIN, control=_CONTROL, read=""))
    assert separate == []


def test_head_is_not_served_so_it_cannot_bypass_a_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FastAPI routes do not add HEAD to GET routes; a route that starts serving HEAD must inherit its GET scope."""
    _all_keys(monkeypatch)
    client = _app(tmp_path)
    for method, path in _routes(client):
        if method != "GET" or _expected_scope(method, path) == _RELAY:
            continue
        for key in (None, _READ, _CONTROL, _ADMIN):
            headers = {"X-Domesti-Api-Key": key} if key else {}
            status = client.head(_concrete(path), headers=headers).status_code
            assert status == HTTPStatus.METHOD_NOT_ALLOWED, (path, key, status)
