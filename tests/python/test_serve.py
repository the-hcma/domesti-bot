"""Tests for :mod:`config.serve` resolver and socket binder."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from config import serve as serve_module
from config.serve import (
    bind_listen_socket,
    browser_url_for_auto_open,
    resolve_listen_address,
)


def _ns(host: str | None, port: int | None) -> argparse.Namespace:
    return argparse.Namespace(listen_host=host, listen_port=port)


def _ns_with_browser(
    *, no_browser: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(
        listen_host=None,
        listen_port=None,
        no_browser=no_browser,
    )


def _mock_sock(host: str, port: int) -> socket.socket:
    """Build a Mock socket that reports ``(host, port)`` for ``getsockname()``."""

    sock = MagicMock(spec=socket.socket)
    sock.getsockname.return_value = (host, port)
    return sock


def test_dev_default_is_loopback_port_zero() -> None:
    host, port = resolve_listen_address(_ns(None, None), env={})
    assert host == "127.0.0.1"
    assert port == 0


def test_env_overrides_dev_default() -> None:
    host, port = resolve_listen_address(
        _ns(None, None),
        env={"DOMESTI_LISTEN_HOST": "0.0.0.0", "DOMESTI_LISTEN_PORT": "9001"},
    )
    assert (host, port) == ("0.0.0.0", 9001)


def test_cli_beats_env() -> None:
    host, port = resolve_listen_address(
        _ns("10.0.0.1", 9999),
        env={"DOMESTI_LISTEN_HOST": "0.0.0.0", "DOMESTI_LISTEN_PORT": "1"},
    )
    assert (host, port) == ("10.0.0.1", 9999)


def test_cli_port_zero_is_respected() -> None:
    # Explicit --listen-port 0 must NOT fall through to the env var.
    host, port = resolve_listen_address(
        _ns(None, 0), env={"DOMESTI_LISTEN_PORT": "8765"}
    )
    assert port == 0
    assert host == "127.0.0.1"


def test_invalid_env_port_raises_system_exit() -> None:
    with pytest.raises(SystemExit, match="invalid DOMESTI_LISTEN_PORT"):
        resolve_listen_address(_ns(None, None), env={"DOMESTI_LISTEN_PORT": "abc"})


def test_out_of_range_port_raises_system_exit() -> None:
    with pytest.raises(SystemExit, match="out of range"):
        resolve_listen_address(_ns(None, 70000), env={})


def test_bind_listen_socket_allocates_a_free_port() -> None:
    with bind_listen_socket("127.0.0.1", 0) as sock:
        bound_host, bound_port = sock.getsockname()[:2]
        assert bound_host == "127.0.0.1"
        assert 1024 <= bound_port <= 65535, "OS must pick a real ephemeral port"
        assert sock.fileno() != -1


def test_bind_listen_socket_specific_port_round_trip() -> None:
    # Bind to port 0 first to discover a free port, close, then re-bind to it.
    with bind_listen_socket("127.0.0.1", 0) as probe:
        _, free_port = probe.getsockname()[:2]

    with bind_listen_socket("127.0.0.1", free_port) as sock:
        assert sock.getsockname()[1] == free_port


def test_bind_listen_socket_in_use_raises_with_helpful_message() -> None:
    with bind_listen_socket("127.0.0.1", 0) as held:
        _, busy_port = held.getsockname()[:2]
        with pytest.raises(SystemExit, match="already in use"):
            bind_listen_socket("127.0.0.1", busy_port)


def test_bind_listen_socket_returns_streaming_listener() -> None:
    with bind_listen_socket("127.0.0.1", 0) as sock:
        assert sock.type == socket.SOCK_STREAM
        # ``listen()`` must have been called; a second listen() is a no-op but
        # accepting a connection requires the socket to be in LISTEN state.
        with socket.create_connection(sock.getsockname(), timeout=1) as client:
            conn, _ = sock.accept()
            conn.close()
            assert client.fileno() != -1


def test_main_swallows_keyboard_interrupt_and_logs_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ctrl-C must produce a single info line, not a chained traceback."""

    monkeypatch.setattr(serve_module, "apply_logging_from_env", lambda: {})
    monkeypatch.setattr(
        serve_module,
        "build_serve_parser",
        lambda: _StubParser(),
    )
    monkeypatch.setattr(serve_module, "create_app", lambda args: object())
    monkeypatch.setattr(serve_module, "_log_listening_banner", lambda sock: None)

    bound_sockets: list[socket.socket] = []

    def _fake_bind(host: str, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        bound_sockets.append(s)
        return s

    monkeypatch.setattr(serve_module, "bind_listen_socket", _fake_bind)

    class _FakeServer:
        def __init__(self, config: object) -> None:
            self.config = config

        async def serve(self, sockets: list[socket.socket]) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(serve_module.uvicorn, "Server", _FakeServer)

    try:
        with caplog.at_level(logging.INFO, logger="config.serve"):
            serve_module.main()  # MUST NOT raise.
    finally:
        for s in bound_sockets:
            s.close()

    messages = [r.getMessage() for r in caplog.records if r.name == "config.serve"]
    assert any("stopped by user" in m for m in messages), messages


class _StubParser:
    """Minimal argparse-shaped stub for the KeyboardInterrupt test above."""

    def parse_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            listen_host=None,
            listen_port=None,
            no_discovery_cache=True,
            discovery_cache=None,
            # KeyboardInterrupt path: don't kick off the browser-open
            # task. ``_FakeServer`` below has no ``started`` attribute
            # and would crash the open-task otherwise.
            no_browser=True,
        )


def test_browser_url_for_auto_open_returns_loopback_url_by_default() -> None:
    sock = _mock_sock("127.0.0.1", 12345)
    url = browser_url_for_auto_open(_ns_with_browser(), sock, env={})
    assert url == "http://127.0.0.1:12345/"


def test_browser_url_for_auto_open_uses_ipv4_form_for_ipv6_loopback() -> None:
    # Bound on ``::1`` should still produce the IPv4 URL — every
    # browser handles it, and IPv6 loopback URLs read awkwardly.
    sock = _mock_sock("::1", 8765)
    url = browser_url_for_auto_open(_ns_with_browser(), sock, env={})
    assert url == "http://127.0.0.1:8765/"


def test_browser_url_for_auto_open_returns_none_when_no_browser_flag() -> None:
    sock = _mock_sock("127.0.0.1", 12345)
    url = browser_url_for_auto_open(
        _ns_with_browser(no_browser=True), sock, env={}
    )
    assert url is None


def test_browser_url_for_auto_open_returns_none_under_systemd() -> None:
    # systemd sets INVOCATION_ID for every unit; treat that as a signal
    # to skip auto-open regardless of bind address.
    sock = _mock_sock("127.0.0.1", 12345)
    url = browser_url_for_auto_open(
        _ns_with_browser(), sock, env={"INVOCATION_ID": "abc123"}
    )
    assert url is None


def test_browser_url_for_auto_open_returns_none_for_wildcard_bind() -> None:
    # ``0.0.0.0`` is for LAN serving — no single "right" URL to open.
    sock = _mock_sock("0.0.0.0", 8765)
    url = browser_url_for_auto_open(_ns_with_browser(), sock, env={})
    assert url is None


def test_browser_url_for_auto_open_returns_none_for_specific_lan_ip() -> None:
    sock = _mock_sock("192.168.1.50", 8765)
    url = browser_url_for_auto_open(_ns_with_browser(), sock, env={})
    assert url is None


def test_browser_url_for_auto_open_handles_missing_no_browser_attr() -> None:
    # Pre-existing callers (e.g. older tests) build a Namespace without
    # ``no_browser``. ``getattr(args, "no_browser", False)`` must not
    # raise — the auto-open should default to ON for new-style callers
    # and stay ON for old-style callers that just don't know about it.
    sock = _mock_sock("127.0.0.1", 12345)
    args = argparse.Namespace(listen_host=None, listen_port=None)
    url = browser_url_for_auto_open(args, sock, env={})
    assert url == "http://127.0.0.1:12345/"


@pytest.mark.asyncio
async def test_open_browser_after_server_ready_calls_webbrowser_open_when_started() -> None:
    server: Any = MagicMock()
    server.started = False

    async def _flip_started() -> None:
        await asyncio.sleep(0.05)
        server.started = True

    flip_task = asyncio.create_task(_flip_started())
    with patch.object(serve_module.webbrowser, "open", return_value=True) as wb_open:
        await asyncio.wait_for(
            serve_module._open_browser_after_server_ready(
                server, "http://127.0.0.1:12345/", timeout_s=1.0
            ),
            timeout=2.0,
        )
    await flip_task
    wb_open.assert_called_once_with("http://127.0.0.1:12345/", new=2)


@pytest.mark.asyncio
async def test_open_browser_after_server_ready_gives_up_on_timeout() -> None:
    server: Any = MagicMock()
    server.started = False  # stays False forever
    with patch.object(serve_module.webbrowser, "open") as wb_open:
        await asyncio.wait_for(
            serve_module._open_browser_after_server_ready(
                server, "http://127.0.0.1:12345/", timeout_s=0.1
            ),
            timeout=1.0,
        )
    wb_open.assert_not_called()


@pytest.mark.asyncio
async def test_open_browser_after_server_ready_swallows_webbrowser_error() -> None:
    server: Any = MagicMock()
    server.started = True
    with patch.object(
        serve_module.webbrowser, "open", side_effect=RuntimeError("no DISPLAY")
    ):
        # MUST NOT raise — a missing $DISPLAY shouldn't take the server
        # launcher down.
        await asyncio.wait_for(
            serve_module._open_browser_after_server_ready(
                server, "http://127.0.0.1:12345/", timeout_s=0.5
            ),
            timeout=1.0,
        )
