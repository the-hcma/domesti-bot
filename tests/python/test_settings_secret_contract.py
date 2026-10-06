"""Write-only secrets contract for ``/v1/settings``.

Settings endpoints must not return secret material on read. This module holds three guards:

* an OpenAPI contract test over every ``/v1/settings`` GET response schema;
* a sentinel test that stores recognizable fake secrets and asserts they never appear in a GET response;
* a validation-error test that a rejected secret is not echoed in the 422 body or in logs.

Every settings endpoint is write-only (domesti-bot#727), so there is no allowlist of endpoints that may still
read a secret back: any secret-bearing string property in a response, or any seeded secret in a GET body, fails.
Only identifiers (see ``_IDENTIFIER_ALLOWLIST``) may be returned.
"""

from __future__ import annotations

import argparse
import logging
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.schemas import KasaCredentialsSetIn
from app.db.secrets import (
    load_mytracks_relay_api_key_from_db,
    save_mytracks_admin_password_to_db,
    save_mytracks_relay_api_key_to_db,
)
from app.mytracks_service import MyTracksPairResult, MyTracksSyncError
from app.vizio_smartcast_client import VizioDeviceInfoSnapshot

_SETTINGS_PREFIX = "/v1/settings"

# A string-ish property whose name contains one of these (or starts with ``stored_``) carries a secret.
_SECRET_NAME = re.compile(r"(pass|token|secret|psk|key|auth|cred|bearer|(^|_)pin($|_))", re.IGNORECASE)

# Names that match but are not secrets: ``secrets_key_source`` / ``auth_source`` hold "env" / "database" / ...,
# and the Kasa host lists hold LAN addresses.
_NON_SECRET_PROPERTY_NAMES = frozenset(
    {"secrets_key_source", "auth_source", "hosts_requiring_klap_auth", "skipped_auth_hosts"}
)

# Identifiers (not credentials) that may be returned: shown so the operator can tell which account is set.
_IDENTIFIER_ALLOWLIST = frozenset({("KasaCredentialsSettingsOut", "stored_username")})

# Built rather than written as literals: gitleaks' generic-api-key rule flags a literal next to a
# ``token`` / ``key`` name and would fail the secret-scan job.
_SENTINEL_NAMES = (
    "kasa_password",
    "tailwind_token",
    "ep1_noise_psk",
    "vizio_token",
    "relay_key",
    "mytracks_admin_password",
    "smtp_password",
)
_SENTINELS = {name: "-".join(["SENTINEL", name.replace("_", "-"), "value"]) for name in _SENTINEL_NAMES}
_REJECTED_PREFIX = "SENTINEL-rejected-secret"

# GET paths that legitimately answer something other than 200 in this fixture (nothing discovered yet).
_EXPECTED_GET_STATUSES: dict[str, set[int]] = {
    "/v1/settings/discovery": {HTTPStatus.SERVICE_UNAVAILABLE},
}


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)


def _app(cache_path: Path) -> FastAPI:
    args = argparse.Namespace(
        discovery_cache=str(cache_path),
        tailwind_token=None,
        vizio_auth_token=None,
        vizio_host=[],
        no_vizio=False,
    )
    return create_app(args)


def _client(cache_path: Path) -> TestClient:
    return TestClient(_app(cache_path))


def _resolve(schema: dict[str, Any], components: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        return name, components[name]
    return None, schema


def _is_stringish(schema: dict[str, Any], components: dict[str, Any]) -> bool:
    """A string, or a list / mapping of strings, or a ``$ref`` to a string alias or enum."""
    _name, resolved = _resolve(schema, components)
    if resolved.get("type") == "string" or "enum" in resolved:
        return resolved.get("format") not in {"date-time", "date"}
    if resolved.get("type") == "array":
        return _is_stringish(resolved.get("items", {}), components)
    if resolved.get("type") == "object" and isinstance(resolved.get("additionalProperties"), dict):
        return _is_stringish(resolved["additionalProperties"], components)
    members = [m for key in ("anyOf", "oneOf", "allOf") for m in resolved.get(key, []) if isinstance(m, dict)]
    if members:
        return any(_is_stringish(m, components) for m in members)
    # No type information (an ``Any`` field) could carry a string, so it is treated as one.
    return "type" not in resolved and "properties" not in resolved


def _is_secret_property(name: str) -> bool:
    return name not in _NON_SECRET_PROPERTY_NAMES and (name.startswith("stored_") or bool(_SECRET_NAME.search(name)))


def _object_properties(
    schema: dict[str, Any],
    components: dict[str, Any],
    owner: str | None,
    seen: set[str],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Every ``(component, property, schema)`` reachable from ``schema``."""
    name, resolved = _resolve(schema, components)
    owner = name or owner
    found: list[tuple[str, str, dict[str, Any]]] = []
    if name is not None:
        if name in seen:
            return found
        seen = seen | {name}
    for prop, prop_schema in resolved.get("properties", {}).items():
        found.append((owner or "<inline>", prop, prop_schema))
        found.extend(_object_properties(prop_schema, components, owner, seen))
    for key in ("items", "additionalProperties"):
        nested = resolved.get(key)
        if isinstance(nested, dict):
            found.extend(_object_properties(nested, components, owner, seen))
    for key in ("anyOf", "oneOf", "allOf"):
        for member in resolved.get(key, []):
            found.extend(_object_properties(member, components, owner, seen))
    return found


def _settings_operations(spec: dict[str, Any], method: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (path, item[method])
        for path, item in spec["paths"].items()
        if (path == _SETTINGS_PREFIX or path.startswith(_SETTINGS_PREFIX + "/")) and method in item
    ]


def _response_schemas(operation: dict[str, Any]) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for response in operation.get("responses", {}).values():
        content = response.get("content", {}).get("application/json", {})
        if "schema" in content:
            schemas.append(content["schema"])
    return schemas


def test_settings_responses_do_not_return_secret_properties(tmp_path: Path) -> None:
    spec = _app(tmp_path / "ui.sqlite").openapi()
    components = spec["components"]["schemas"]
    violations: list[str] = []
    exempted: set[tuple[str, str]] = set()
    for method in ("get", "post", "put", "patch", "delete"):
        for path, operation in _settings_operations(spec, method):
            for response_schema in _response_schemas(operation):
                for component, prop, prop_schema in _object_properties(response_schema, components, None, set()):
                    if not _is_stringish(prop_schema, components) or not _is_secret_property(prop):
                        continue
                    if (component, prop) in _IDENTIFIER_ALLOWLIST:
                        exempted.add((component, prop))
                        continue
                    violations.append(f"{method.upper()} {path}: {component}.{prop}")
    assert not violations, "secret-bearing response properties must be write-only: " + ", ".join(violations)
    # An exemption that no response reaches is dead weight that could mask a new readback.
    assert exempted == _IDENTIFIER_ALLOWLIST


def test_settings_request_secret_fields_are_marked_write_only(tmp_path: Path) -> None:
    spec = _app(tmp_path / "ui.sqlite").openapi()
    components = spec["components"]["schemas"]
    unmarked: list[str] = []
    for method in ("post", "put", "patch"):
        for path, operation in _settings_operations(spec, method):
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
            if "schema" not in body:
                continue
            for component, prop, prop_schema in _object_properties(body["schema"], components, None, set()):
                is_secret = _is_stringish(prop_schema, components) and _is_secret_property(prop)
                if is_secret and not prop_schema.get("writeOnly"):
                    unmarked.append(f"{method.upper()} {path}: {component}.{prop}")
    assert not unmarked, "request secret fields need writeOnly: " + ", ".join(sorted(set(unmarked)))


def _seed_secrets(client: TestClient, db: Path) -> list[str]:
    """Store every kind of secret; return the text of every write response so it can be checked for leaks."""
    write_responses: list[str] = []

    def _record(response: Any) -> None:
        assert response.status_code == HTTPStatus.OK
        write_responses.append(response.text)

    _record(
        client.put(
            "/v1/settings/kasa-credentials",
            json={"username": "alice@example.com", "password": _SENTINELS["kasa_password"]},
        )
    )
    _record(client.put("/v1/settings/tailwind-token", json={"token": _SENTINELS["tailwind_token"]}))
    _record(client.put("/v1/settings/ep1-noise-psk", json={"noise_psk": _SENTINELS["ep1_noise_psk"]}))
    info = VizioDeviceInfoSnapshot(model_name="V505M-K09", cast_name="Kitchen TV", diid="abc", mac="00:bd:3e:d5:f0:11")
    with (
        patch(
            "app.api.vizio_settings_routes.VizioSmartCastClient.fetch_deviceinfo",
            new_callable=AsyncMock,
            return_value=info,
        ),
        patch(
            "app.api.vizio_settings_routes.resolve_vizio_tv_mac",
            new_callable=AsyncMock,
            return_value="00:bd:3e:d5:f0:11",
        ),
    ):
        _record(client.put("/v1/settings/vizio/tvs/192.168.86.201/auth", json={"token": _SENTINELS["vizio_token"]}))
    save_mytracks_relay_api_key_to_db(db, _SENTINELS["relay_key"])
    save_mytracks_admin_password_to_db(db, _SENTINELS["mytracks_admin_password"])
    _record(
        client.put(
            "/v1/settings/smtp",
            json={
                "from_address": "bot@example.com",
                "host": "smtp.example.com",
                "mail_domain": "example.com",
                "port": 587,
                "username": "bot",
                "password": _SENTINELS["smtp_password"],
            },
        )
    )
    return write_responses


def _pair(client: TestClient) -> str:
    """Pair through the real route (network calls patched); return the response text."""
    with (
        patch(
            "app.api.mytracks_routes.pair_with_my_tracks", return_value=MyTracksPairResult(status_code=HTTPStatus.OK)
        ),
        patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", side_effect=MyTracksSyncError("offline")),
    ):
        response = client.post(
            "/v1/settings/my-tracks/pair",
            json={
                "domain": "https://tracks.example.com",
                "username": "admin",
                "password": _SENTINELS["mytracks_admin_password"],
            },
        )
    assert response.status_code == HTTPStatus.OK
    return response.text


def _scan_gets(client: TestClient, paths: list[str], secrets_to_find: dict[str, str]) -> dict[str, set[str]]:
    leaks: dict[str, set[str]] = {}
    for path in paths:
        response = client.get(path)
        assert response.status_code in _EXPECTED_GET_STATUSES.get(path, {HTTPStatus.OK}), (
            f"GET {path} returned {response.status_code}, so it could not have shown a leak"
        )
        for name, value in secrets_to_find.items():
            if value in response.text:
                leaks.setdefault(path, set()).add(name)
    return leaks


def test_settings_get_responses_never_contain_stored_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    for env_name in ("VIZIO_AUTH_TOKEN", "EP1_NOISE_PSK", "TAILWIND_TOKEN"):
        monkeypatch.delenv(env_name, raising=False)
    db = tmp_path / "ui.sqlite"
    app = _app(db)
    client = TestClient(app)
    write_responses = _seed_secrets(client, db)

    spec = app.openapi()
    get_paths = sorted(path for path, _op in _settings_operations(spec, "get") if "{" not in path)
    assert "/v1/settings/vizio/tvs" in get_paths
    assert "/v1/settings/smtp" in get_paths

    # Scan once with only the seeded sentinels stored, and again after pairing has generated a relay key.
    leaks = _scan_gets(client, get_paths, _SENTINELS)
    write_responses.append(_pair(client))
    generated_relay_key = load_mytracks_relay_api_key_from_db(db)
    assert generated_relay_key is not None
    assert generated_relay_key != _SENTINELS["relay_key"]
    secrets_to_find = {**_SENTINELS, "generated_relay_key": generated_relay_key}
    for path, names in _scan_gets(client, get_paths, secrets_to_find).items():
        leaks.setdefault(path, set()).update(names)

    # Clearing must not echo what was stored either.
    for delete_path in ("/v1/settings/kasa-credentials", "/v1/settings/tailwind-token", "/v1/settings/ep1-noise-psk"):
        response = client.delete(delete_path)
        assert response.status_code == HTTPStatus.OK
        write_responses.append(response.text)

    for text in write_responses:
        for name, value in secrets_to_find.items():
            if value in text:
                leaks.setdefault("<write response>", set()).add(name)

    assert not leaks, f"settings responses leak stored secrets: {leaks}"


_OVERSIZED = _REJECTED_PREFIX + "-" + "x" * 300


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/settings/kasa-credentials", {"username": "alice@example.com", "password": _OVERSIZED}),
        ("/v1/settings/tailwind-token", {"token": _OVERSIZED}),
        ("/v1/settings/ep1-noise-psk", {"noise_psk": _OVERSIZED}),
        ("/v1/settings/vizio/tvs/192.168.86.201/auth", {"token": _OVERSIZED}),
    ],
)
def test_rejected_secret_is_not_echoed_in_422_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    path: str,
    payload: dict[str, str],
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client = _client(tmp_path / "ui.sqlite")
    with caplog.at_level(logging.DEBUG):
        response = client.put(path, json=payload)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert _REJECTED_PREFIX not in response.text
    for error in response.json()["detail"]:
        assert set(error) == {"type", "loc", "msg"}
    assert _REJECTED_PREFIX not in caplog.text


def test_missing_required_field_422_does_not_echo_the_rest_of_the_request_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing required field makes FastAPI's default handler echo the whole body as ``input``."""
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client = _client(tmp_path / "ui.sqlite")
    response = client.put("/v1/settings/tailwind-token", json={"note": _SENTINELS["tailwind_token"]})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert _SENTINELS["tailwind_token"] not in response.text
    assert [error["type"] for error in response.json()["detail"]] == ["missing"]


def test_default_fastapi_handler_would_echo_the_rejected_secret() -> None:
    """Control: the same model behind FastAPI's default 422 handler returns the secret, so the guards above bite."""
    bare = FastAPI()

    @bare.put("/probe")
    def probe(body: KasaCredentialsSetIn) -> dict[str, bool]:
        del body
        return {"ok": True}

    response = TestClient(bare).put("/probe", json={"username": "alice@example.com", "password": _OVERSIZED})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert _REJECTED_PREFIX in response.text


def test_every_settings_write_with_a_secret_field_rejects_bad_input_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    app = _app(tmp_path / "ui.sqlite")
    client = TestClient(app)
    spec = app.openapi()
    components = spec["components"]["schemas"]
    exercised = 0
    leaks: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for method in ("post", "put", "patch"):
            for path, operation in _settings_operations(spec, method):
                body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
                if "schema" not in body:
                    continue
                secret_fields = [
                    prop
                    for _component, prop, prop_schema in _object_properties(body["schema"], components, None, set())
                    if _is_secret_property(prop) and _is_stringish(prop_schema, components)
                ]
                for field in sorted(set(secret_fields)):
                    # A list where a string is expected is a type error on every endpoint, so the 422 path is hit.
                    response = client.request(
                        method.upper(), re.sub(r"\{[^}]+\}", "x", path), json={field: [_OVERSIZED]}
                    )
                    exercised += 1
                    if response.status_code != HTTPStatus.UNPROCESSABLE_ENTITY or _REJECTED_PREFIX in response.text:
                        leaks.append(f"{method.upper()} {path} {field}: {response.status_code}")
    assert exercised >= 10
    assert not leaks, f"settings writes echoed a rejected secret or did not return 422: {leaks}"
    assert _REJECTED_PREFIX not in caplog.text


def test_request_secret_fields_are_excluded_from_model_repr() -> None:
    model = KasaCredentialsSetIn(username="alice@example.com", password=_SENTINELS["kasa_password"])
    assert _SENTINELS["kasa_password"] not in repr(model)
    assert re.search(r"username=", repr(model)) is not None
