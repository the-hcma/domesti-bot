"""Run the domesti HTTP API (FastAPI + uvicorn) for LAN device control.

Logging is installed before uvicorn boots so that uvicorn's internal loggers
(``uvicorn``, ``uvicorn.error``, ``uvicorn.access``) flow through the same
formatter and handlers as the application. See :mod:`app.logging_config` for
the dict-config and ``scripts/domesti-bot-server`` for the env vars that
drive it.

**Dev-mode default** (no flags, no env vars): bind to ``127.0.0.1`` on an
**OS-allocated free port** and print the actual URL at startup. This mirrors
the fpdf launcher pattern: it is impossible to clash with anything else
listening (e.g. another local dev server, the Cursor my-tracks plugin) and the
URL is shown explicitly so the developer can paste it into a browser. For
production, the systemd unit passes ``--listen-host 127.0.0.1 --listen-port
8765`` explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import socket
import time
import webbrowser

import uvicorn

from app.api.app import create_app
from app.domesti_bot_cli import build_arg_parser
from app.logging_config import apply_logging_from_env


_LOGGER = logging.getLogger(__name__)

# How long to wait for uvicorn to flip ``server.started=True`` before
# giving up on auto-opening a browser. Generous: lifespan startup is
# very fast (discovery is deferred to a background task), but if the
# user has an unusually slow ASGI middleware chain we'd rather skip the
# browser than freeze waiting for it.
_BROWSER_OPEN_TIMEOUT_S: float = 5.0


def build_serve_parser() -> argparse.ArgumentParser:
    parent = build_arg_parser(add_help=False)
    parser = argparse.ArgumentParser(
        description=(
            "Start the domesti device-control HTTP API (same discovery flags as the REPL CLI)."
        ),
        parents=[parent],
    )
    parser.add_argument(
        "--listen-host",
        default=None,
        metavar="ADDR",
        help=(
            "Bind address. If unset, falls back to $DOMESTI_LISTEN_HOST, "
            "then to 127.0.0.1 (dev mode). Use 0.0.0.0 for LAN access."
        ),
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=None,
        metavar="PORT",
        help=(
            "Listen port. If unset, falls back to $DOMESTI_LISTEN_PORT, then to "
            "0 (let the OS pick a free port; the actual port is printed at startup)."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=(
            "Do not auto-open a browser window after the server starts. "
            "Default behavior: open ``http://127.0.0.1:<port>/`` once "
            "uvicorn is serving, when bound to a loopback address and "
            "not running under systemd."
        ),
    )
    return parser


def resolve_listen_address(
    args: argparse.Namespace,
    *,
    env: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Pick the effective ``(host, port)`` from CLI flags, env vars, or dev defaults.

    Precedence: explicit CLI flag → env var (``DOMESTI_LISTEN_HOST`` /
    ``DOMESTI_LISTEN_PORT``) → dev default (``127.0.0.1``, port ``0``).
    """

    env = env if env is not None else dict(os.environ)
    cli_host: str | None = getattr(args, "listen_host", None)
    cli_port: int | None = getattr(args, "listen_port", None)

    env_host = (env.get("DOMESTI_LISTEN_HOST") or "").strip()
    env_port_raw = (env.get("DOMESTI_LISTEN_PORT") or "").strip()

    host = cli_host if cli_host is not None else (env_host or "127.0.0.1")

    if cli_port is not None:
        port = cli_port
    elif env_port_raw:
        try:
            port = int(env_port_raw)
        except ValueError as exc:
            raise SystemExit(
                f"domesti-bot-server: invalid DOMESTI_LISTEN_PORT={env_port_raw!r}"
            ) from exc
    else:
        port = 0

    if not 0 <= port <= 65535:
        raise SystemExit(
            f"domesti-bot-server: --listen-port out of range (0..65535): {port}"
        )

    return host, port


def bind_listen_socket(host: str, port: int) -> socket.socket:
    """Create, bind, and ``listen()`` on a TCP socket — fpdf-style early bind.

    The socket is handed to ``uvicorn.Server.serve(sockets=[sock])`` so the OS
    picks the free port *before* application startup (lifespan / discovery)
    runs, which lets us log the actual URL at the very top of the run.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        if port != 0:
            raise SystemExit(
                f"domesti-bot-server: port {host}:{port} is already in use "
                f"({exc.strerror or exc}). Choose a different --listen-port, "
                f"or omit --listen-port to let the OS pick a free port."
            ) from exc
        raise SystemExit(
            f"domesti-bot-server: failed to bind {host}:0 ({exc.strerror or exc})"
        ) from exc
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def _lan_addresses() -> list[str]:
    """Best-effort enumeration of non-loopback IPv4 addresses for banner output."""

    addresses: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            sockaddr = info[4]
            if not sockaddr:
                continue
            ip = str(sockaddr[0])
            if ip and not ip.startswith("127.") and ip not in addresses:
                addresses.append(ip)
    except OSError:
        pass
    return addresses


def browser_url_for_auto_open(
    args: argparse.Namespace,
    sock: socket.socket,
    *,
    env: dict[str, str] | None = None,
) -> str | None:
    """Return the URL to auto-open in a browser, or ``None`` to skip.

    Skips when *any* of the following hold:

    * ``--no-browser`` was passed (explicit opt-out);
    * ``$INVOCATION_ID`` is set (we're running under a systemd unit —
      auto-launching a browser from a headless service is pointless);
    * the socket isn't bound to a loopback address. ``0.0.0.0`` / ``::``
      bindings are for headless / LAN serving where there's no single
      "right" URL to pick, and explicit LAN-IP bindings are usually a
      remote-admin scenario where ``DISPLAY`` won't reach the user.

    Always returns ``http://127.0.0.1:<port>/`` (never ``::1``) for the
    loopback case — the IPv4 form works in every browser on every host
    we ship to.
    """

    env = env if env is not None else dict(os.environ)
    if getattr(args, "no_browser", False):
        return None
    if env.get("INVOCATION_ID"):
        return None
    bound_host, bound_port = sock.getsockname()[:2]
    if bound_host not in ("127.0.0.1", "::1"):
        return None
    return f"http://127.0.0.1:{bound_port}/"


async def _open_browser_after_server_ready(
    server: uvicorn.Server,
    url: str,
    *,
    timeout_s: float = _BROWSER_OPEN_TIMEOUT_S,
) -> None:
    """Poll ``server.started`` then ``webbrowser.open(url)`` once it's True.

    We wait for uvicorn to flip the flag rather than opening immediately
    so the user's first ``GET /`` lands on a serving process instead of
    the kernel listen-queue holding the connection. If the flag never
    flips within ``timeout_s`` we log and give up — better than hanging
    the launcher.
    """

    deadline = time.monotonic() + timeout_s
    while not server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if not server.started:
        _LOGGER.warning(
            "[http] browser-open skipped: server not ready within %.1fs",
            timeout_s,
        )
        return
    try:
        opened = webbrowser.open(url, new=2)
    except Exception:
        _LOGGER.warning("[http] browser-open failed for %s", url, exc_info=True)
        return
    if opened:
        _LOGGER.info("[http] opened browser at %s", url)
    else:
        _LOGGER.info(
            "[http] no browser available on this host; visit %s manually",
            url,
        )


def _log_listening_banner(sock: socket.socket) -> None:
    bound_host, bound_port = sock.getsockname()[:2]
    api_key_state = "set" if (os.environ.get("DOMESTI_API_KEY") or "").strip() else "unset"
    _LOGGER.info(
        "[http] listening on http://%s:%d (api-key %s)",
        bound_host,
        bound_port,
        api_key_state,
    )
    if bound_host in ("0.0.0.0", "::"):
        _LOGGER.info("[http] local:   http://127.0.0.1:%d", bound_port)
        for ip in _lan_addresses():
            _LOGGER.info("[http] network: http://%s:%d", ip, bound_port)


def main() -> None:
    apply_logging_from_env()
    parser = build_serve_parser()
    args = parser.parse_args()
    if args.no_discovery_cache:
        args.discovery_cache = None

    host, port = resolve_listen_address(args)
    sock = bind_listen_socket(host, port)
    _log_listening_banner(sock)

    app = create_app(args)
    config = uvicorn.Config(
        app,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    browser_url = browser_url_for_auto_open(args, sock)

    async def _runner() -> None:
        # Race the browser-open against ``server.serve()``: opening before
        # uvicorn flips ``server.started`` would land on the kernel's
        # listen queue and stall the first GET / until the lifespan ran.
        open_task: asyncio.Task[None] | None = None
        if browser_url is not None:
            open_task = asyncio.create_task(
                _open_browser_after_server_ready(server, browser_url),
                name="open-browser",
            )
        try:
            await server.serve(sockets=[sock])
        finally:
            if open_task is not None and not open_task.done():
                open_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await open_task

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        # Ctrl-C (or any SIGINT delivered to the foreground process group):
        # asyncio's runner re-raises KeyboardInterrupt after cancelling the
        # serve task. uvicorn has already run its graceful shutdown by this
        # point; swallow the exception so the user sees a single tidy line
        # instead of a chained CancelledError → KeyboardInterrupt traceback.
        _LOGGER.info("[http] stopped by user (SIGINT)")


if __name__ == "__main__":
    main()
