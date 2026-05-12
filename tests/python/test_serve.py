"""Tests for :mod:`config.serve` resolver and socket binder."""

from __future__ import annotations

import argparse
import logging
import socket
from unittest.mock import patch

import pytest

from config import serve as serve_module
from config.serve import bind_listen_socket, resolve_listen_address


def _ns(host: str | None, port: int | None) -> argparse.Namespace:
    return argparse.Namespace(listen_host=host, listen_port=port)


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
    sock = bind_listen_socket("127.0.0.1", 0)
    try:
        bound_host, bound_port = sock.getsockname()[:2]
        assert bound_host == "127.0.0.1"
        assert 1024 <= bound_port <= 65535, "OS must pick a real ephemeral port"
        assert sock.fileno() != -1
    finally:
        sock.close()


def test_bind_listen_socket_specific_port_round_trip() -> None:
    # Bind to port 0 first to discover a free port, close, then re-bind to it.
    probe = bind_listen_socket("127.0.0.1", 0)
    _, free_port = probe.getsockname()[:2]
    probe.close()

    sock = bind_listen_socket("127.0.0.1", free_port)
    try:
        assert sock.getsockname()[1] == free_port
    finally:
        sock.close()


def test_bind_listen_socket_in_use_raises_with_helpful_message() -> None:
    held = bind_listen_socket("127.0.0.1", 0)
    try:
        _, busy_port = held.getsockname()[:2]
        with pytest.raises(SystemExit, match="already in use"):
            bind_listen_socket("127.0.0.1", busy_port)
    finally:
        held.close()


def test_bind_listen_socket_returns_streaming_listener() -> None:
    sock = bind_listen_socket("127.0.0.1", 0)
    try:
        assert sock.type == socket.SOCK_STREAM
        # ``listen()`` must have been called; a second listen() is a no-op but
        # accepting a connection requires the socket to be in LISTEN state.
        with socket.create_connection(sock.getsockname(), timeout=1) as client:
            conn, _ = sock.accept()
            conn.close()
            assert client.fileno() != -1
    finally:
        sock.close()


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
        )
