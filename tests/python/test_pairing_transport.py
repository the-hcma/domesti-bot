"""Plain-HTTP policy for the My Tracks pairing URLs: HTTPS always, HTTP on a LAN with a warning, never public."""

from __future__ import annotations

import argparse
import socket
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.mytracks_service import DomestiBotConfigFromMyTracks, MyTracksPairResult
from app.pairing_transport import assess_url, transport_refusal, transport_warning, transport_warnings


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://tracks.example.com", "https"),
        ("HTTPS://Tracks.Example.com:8443/path", "https"),
        ("http://localhost:8003", "loopback"),
        ("http://127.0.0.1:8003", "loopback"),
        ("http://[::1]:8003", "loopback"),
        ("http://app.localhost", "loopback"),
        ("http://192.168.1.10:8003", "lan"),
        ("http://10.0.0.5", "lan"),
        ("http://172.16.4.2", "lan"),
        ("http://169.254.10.10", "lan"),
        ("http://[fd00::1]:8003", "lan"),
        ("http://mytracks", "lan"),
        ("http://nas.local", "lan"),
        ("http://tracks.lan", "lan"),
        ("http://tracks.home.arpa", "lan"),
        ("http://svc.internal", "lan"),
        ("http://tracks.example.com", "public"),
        ("http://8.8.8.8", "public"),
        ("http://[2001:4860:4860::8888]", "public"),
        ("http://192.168.1.10.example.com", "public"),
        # numeric spellings that resolvers expand are never trusted as local
        ("http://134744072", "public"),
        ("http://0x08080808", "public"),
        ("http://2130706433", "public"),
        ("http://0x7f.1", "public"),
        # shared address space (CGNAT, Tailscale) is LAN-like; just outside it is public
        ("http://100.64.1.2:8000", "lan"),
        ("http://100.127.255.254", "lan"),
        ("http://100.128.0.1", "public"),
        ("http://100.63.255.255", "public"),
        # trailing dots, case, zone ids, IPv4-mapped IPv6
        ("http://nas.local.", "lan"),
        ("http://localhost.", "loopback"),
        ("http://192.168.1.1.", "lan"),
        ("http://NAS.LOCAL", "lan"),
        ("http://[fe80::1%25eth0]", "lan"),
        ("http://[::ffff:192.168.1.1]", "lan"),
        ("http://[::ffff:8.8.8.8]", "public"),
        # hostnames that only look local
        ("http://localhost.evil.com", "public"),
        ("http://192.168.1.1@evil.com/", "public"),
        ("http://evil.com#@192.168.1.1", "public"),
    ],
)
def test_urls_are_classified_by_scheme_and_host(url: str, expected: str) -> None:
    assert assess_url(url).url_class == expected


@pytest.mark.parametrize(
    "url",
    ["", "http://", "ftp://tracks.example.com", "tracks.example.com", "https:///x", "http://evil.com\\@192.168.1.1"],
)
def test_urls_without_a_usable_host_or_scheme_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        assess_url(url)


def test_classification_never_does_a_dns_lookup() -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("DNS lookup")

    with patch.object(socket, "getaddrinfo", boom), patch.object(socket, "gethostbyname", boom):
        assert assess_url("http://tracks.example.com").url_class == "public"
        assert assess_url("http://nas.local").url_class == "lan"


def test_only_public_http_is_refused_and_only_lan_http_warns() -> None:
    assert transport_refusal("https://tracks.example.com", label="x") is None
    assert transport_refusal("http://192.168.1.10", label="x") is None
    assert transport_refusal("http://localhost", label="x") is None
    refusal = transport_refusal("http://tracks.example.com", label="My Tracks address")
    assert refusal is not None and "My Tracks address" in refusal and "https://" in refusal

    assert transport_warning("http://192.168.1.10", label="x") is not None
    for quiet in ("https://tracks.example.com", "http://localhost", "http://tracks.example.com"):
        assert transport_warning(quiet, label="x") is None


def test_stored_urls_produce_warnings_including_an_old_public_http_one() -> None:
    warnings = transport_warnings(
        {
            "a": "http://192.168.1.10:8003",
            "b": "https://ok.example.com",
            "c": "http://legacy.example.com",
            "d": None,
            "e": "",
            "f": "not a url",
            "g": "http://localhost",
        }
    )
    assert len(warnings) == 2
    assert "local network" in warnings[0] and "192.168.1.10" in warnings[0]
    assert "public host" in warnings[1] and "re-pair" in warnings[1]


# --- the pair route -----------------------------------------------------------------------------------------

_PAIR_OK = MyTracksPairResult(status_code=HTTPStatus.OK)


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(argparse.Namespace(discovery_cache=str(tmp_path / "ui.sqlite"), tailwind_token=None)))


def _pair(client: TestClient, domain: str, *, headers: dict[str, str] | None = None):
    # No test here may touch the network: the pre-flight config fetch and the pairing call are both replaced.
    with (
        patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", return_value=DomestiBotConfigFromMyTracks()),
        patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK) as pair,
    ):
        response = client.post(
            "/v1/settings/my-tracks/pair",
            json={"domain": domain, "username": "admin", "password": "secret-pw"},
            headers=headers or {},
        )
    return response, pair


def test_pairing_with_plain_http_to_a_public_my_tracks_is_refused_before_anything_is_sent(client: TestClient) -> None:
    response, pair = _pair(client, "http://tracks.example.com")
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "plain HTTP to a public host" in response.json()["detail"]
    assert "My Tracks address" in response.json()["detail"]
    pair.assert_not_called()
    assert client.get("/v1/settings/my-tracks/pair-status").json() is None, "nothing was recorded"


def test_pairing_when_domesti_bot_itself_is_public_over_http_is_refused(client: TestClient) -> None:
    response, pair = _pair(
        client,
        "https://tracks.example.com",
        headers={"x-forwarded-host": "bot.example.com", "x-forwarded-proto": "http"},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "public domesti-bot address" in response.json()["detail"]
    pair.assert_not_called()


def test_pairing_over_https_has_no_transport_warnings(client: TestClient) -> None:
    response, pair = _pair(
        client,
        "https://tracks.example.com",
        headers={"x-forwarded-host": "bot.example.com", "x-forwarded-proto": "https"},
    )
    assert response.status_code == HTTPStatus.OK, response.text
    pair.assert_called_once()
    assert response.json()["transport_warnings"] == []


def test_pairing_over_http_on_the_lan_works_and_the_status_warns_about_it(client: TestClient) -> None:
    response, pair = _pair(
        client,
        "http://192.168.1.20:8000",
        headers={"x-forwarded-host": "192.168.1.30:8003", "x-forwarded-proto": "http"},
    )
    assert response.status_code == HTTPStatus.OK, response.text
    pair.assert_called_once()
    warnings = response.json()["transport_warnings"]
    assert any("My Tracks address" in w and "192.168.1.20" in w for w in warnings)
    assert any("public domesti-bot address" in w for w in warnings)
    assert client.get("/v1/settings/my-tracks/pair-status").json()["transport_warnings"] == warnings


def test_a_bare_host_defaults_to_https_so_it_passes_the_policy(client: TestClient) -> None:
    response, pair = _pair(client, "tracks.example.com", headers={"x-forwarded-host": "bot.example.com"})
    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["transport_warnings"] == []
    assert pair.call_args.kwargs["base_url"] == "https://tracks.example.com"


# --- every path that sends keys or the admin password to the saved address ----------------------------------


def _store_domain(db: Path, domain: str) -> None:
    from app.mytracks_store import MyTracksConfigSave, save_mytracks_config

    save_mytracks_config(db, MyTracksConfigSave(domain=domain, username="admin"))


def test_saving_a_plain_http_public_address_is_refused(client: TestClient) -> None:
    response = client.put("/v1/settings/my-tracks", json={"domain": "http://tracks.example.com", "username": "admin"})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "plain HTTP to a public host" in response.json()["detail"]
    assert (
        client.put(
            "/v1/settings/my-tracks", json={"domain": "http://192.168.1.20:8000", "username": "admin"}
        ).status_code
        == HTTPStatus.OK
    )


def test_an_old_stored_public_http_address_blocks_reconcile_and_the_status_asks_to_re_pair(
    tmp_path: Path, client: TestClient
) -> None:
    db = tmp_path / "ui.sqlite"
    _store_domain(db, "http://tracks.example.com")
    with patch("app.api.mytracks_routes.reconcile_pairing") as reconcile:
        response = client.post("/v1/settings/my-tracks/pair/reconcile", json={"password": "x"})
    assert response.status_code == HTTPStatus.CONFLICT
    assert "saved My Tracks domain is invalid" in response.json()["detail"]
    reconcile.assert_not_called()
    warnings = client.get("/v1/settings/my-tracks/pair-status").json()["transport_warnings"]
    assert any("re-pair" in w and "tracks.example.com" in w for w in warnings)


@pytest.mark.asyncio
async def test_request_location_never_sends_the_relay_key_over_plain_http_to_a_public_host() -> None:
    from app.mytracks_service import request_user_location

    with patch("app.mytracks_service.httpx.AsyncClient") as client_cls:
        result = await request_user_location(
            base_url="http://tracks.example.com", relay_api_key="k" * 43, user_id="henrique", reason="x"
        )
    assert result.status == "error"
    assert result.detail is not None and "plain HTTP to a public host" in result.detail
    client_cls.assert_not_called()


def test_the_refusal_says_how_to_fix_a_tls_proxy_setup() -> None:
    refusal = transport_refusal("http://bot.example.com", label="public domesti-bot address")
    assert refusal is not None and "X-Forwarded-Proto: https" in refusal


def test_an_ambiguous_url_is_a_refusal_not_a_crash() -> None:
    refusal = transport_refusal("http://evil.com\\@192.168.1.1", label="My Tracks address")
    assert refusal is not None and "backslashes" in refusal
