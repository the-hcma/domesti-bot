"""Interactive REPL for Google Cast, TP-Link Kasa switches, Sonos speakers, and GoTailwind doors.

Run::

    uv run python -m app.domesti_bot_cli

Or from the repo root (uses ``uv`` when available so the project venv stays in sync)::

    ./scripts/domesti-bot

Credentials:

* Optional ``KASA_USERNAME`` / ``KASA_PASSWORD`` (both) for Kasa/Tapo KLAP.
* Tailwind **Local Control Key**: ``TAILWIND_TOKEN`` or ``--tailwind-token`` (see
  :mod:`app.gotailwind_device_manager`).
* **Encrypted SQLite secrets** (e.g. Tailwind token saved from the web UI): Fernet key in
  ``domesti-bot.config.json`` at the repo root (gitignored) or ``DOMESTI_BOT_SECRETS_KEY`` in the
  environment. Use the ``setup-secrets`` REPL command to create the JSON file.

* **Sonos**: speakers on your LAN (S1-class UPnP stacks included) via optional ``soco``;
  use ``pause`` / ``resume`` in the REPL. Pass ``--no-sonos`` to skip discovery.

* **Google Cast (Chromecast / Google TV / …)** — **PyChromecast** only (no ADB). ``is-on`` treats
  **media playing** (or buffering) as *on*; ``turn-off`` sends Cast **STOP**; ``turn-on`` only
  resumes if the session is **paused** (otherwise a no-op that refreshes status).
  **Discovery:** Cast mDNS via PyChromecast (**on by default**; disable with ``--no-androidtv-zeroconf``
  or ``ANDROIDTV_ZEROCONF=0`` for **cached / explicit hosts only**). ``discover-androidtv`` runs a
  browse and updates the SQLite cache. Set ``ANDROIDTV_HOSTS`` / ``--androidtv-host`` as **IP or
  hostname hints** (optional port ignored). Pass ``--no-androidtv`` to skip.

Discovery is written to a SQLite file by default (Kasa device configs and the last Tailwind
controller host). Optional **display names** are stored in ``device_display_names`` and are
the preferred CLI labels when set. Use ``--no-discovery-cache`` to disable persistence.

The REPL prompt appears as soon as process wiring is up. LAN discovery runs in the
background (same cache-first / ``--force-discovery`` rules as the HTTP server). Per-family
ready lines print as they land; device commands issued before a family is ready print a
discovery-in-progress error instead of a traceback. After a family succeeds, Kasa preferred
labels are pushed onto vendor aliases once (not a continuous sync). Tab completion shows
``preferred_label (mac)`` and still accepts a typed MAC, label, or combined display string.

Use ``refresh-discovery`` in the REPL to rerun Kasa UDP discovery and reload the Tailwind
door list; ``refresh`` reconnects faster using cached Kasa configs when possible.

Colors default to on when stdout is a TTY; set ``NO_COLOR`` or pass ``--color never`` to
disable them (``--color always`` forces ANSI even when piped).

Line editing defaults to **Vim**-style keys (prompt_toolkit). Use ``--edit-mode emacs`` or
``DEVICE_MANAGER_EDIT_MODE=emacs`` for Emacs bindings. In the REPL, ``edit-mode emacs`` /
``edit-mode vim`` switches modes for the current session.

**Remote REPL:** pass ``--api-base-url http://HOST:PORT`` (or ``DEVICE_MANAGER_API_URL``) to
drive devices through the FastAPI service from :mod:`app.api` / ``config/serve.py`` instead
of local discovery. Optional ``--api-key`` / ``DEVICE_MANAGER_API_KEY`` must match
``DOMESTI_API_KEY`` on the server when that env var is set. Run the API with
``scripts/domesti-bot-server``.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import redirect_stderr, redirect_stdout, suppress
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.patch_stdout import patch_stdout

from app import device_discovery_store
from app.androidtv_device_manager import (
    ANDROIDTV_TEMPORARILY_DISABLED,
    ANDROIDTV_TEMPORARILY_DISABLED_REASON,
    AndroidTvDeviceManager,
    _merge_androidtv_host_specs,
    discover_cast_adb_specs_via_zeroconf,
)
from app.build_info import format_cli_version_line
from app.db.secrets import SecretsConfigurationError, save_kasa_credentials_to_db
from app.db.secrets_key import generate_fernet_key, secrets_json_path, write_secrets_json
from app.device_completion import (
    CompletionAlias,
    completion_alias_matches,
    device_completion_alias,
)
from app.device_display import format_device_display
from app.device_label_conflicts import clear_device_label_conflicts, drain_device_label_conflicts
from app.device_manager import NotInitializedError
from app.ep1_credentials import resolve_ep1_noise_psk
from app.ep1_device_manager import (
    DEFAULT_EP1_API_PORT,
    DEFAULT_EP1_ZEROCONF_TIMEOUT_S,
    Ep1Device,
    Ep1DeviceManager,
    Ep1DiscoveryError,
    discover_ep1_hosts,
)
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_credentials import resolve_kasa_credentials
from app.kasa_device_manager import KasaDeviceManager
from app.sonos_device_manager import SonosDeviceManager
from app.tailwind_credentials import resolve_tailwind_token
from app.vizio_device_manager import (
    VizioDeviceManager,
    configured_vizio_host_specs,
)

_LOGGER = logging.getLogger(__name__)

COMMANDS = (
    "clear-display-name",
    "close-door",
    "discover-androidtv",
    "discover-ep1",
    "edit-mode",
    "exit",
    "help",
    "is-on",
    "is-open",
    "kasa-creds",
    "open-door",
    "pause",
    "quit",
    "read-ep1",
    "refresh",
    "refresh-discovery",
    "resume",
    "set-display-name",
    "setup-secrets",
    "show-devices",
    "turn-off",
    "turn-on",
)

COMPLETION_DISCOVERING_HINT = "discovering…"

DEFAULT_DISCOVERY_DB = Path.home() / ".cache" / "rule-engine" / "device_discovery.sqlite"

DISCOVERY_FAILED_PREFIX = "Device discovery failed: "
DISCOVERY_IN_PROGRESS_MSG = "Device discovery still in progress; wait for the family ready line and retry."

EP1_NOT_INITIALIZED_MSG = "EP1 manager is not initialized — try refresh-discovery or discover-ep1."
EP1_NOT_LOADED_MSG = "EP1 not loaded — set --ep1-host / EP1_HOSTS, allow mDNS discovery, or run discover-ep1 first."
EP1_READ_FAILED_PREFIX = "EP1 read failed for "
FAMILY_SKIPPED_NOT_LOADED = "not loaded"
NO_BACKENDS_DEVICE_COMMANDS_UNAVAILABLE_MSG = "No backends initialized; device commands stay unavailable."
NO_BACKENDS_EXITING_MSG = "No backends initialized; exiting."
REFRESH_DONE_PREFIX = "Refreshed"

_COMMAND_HELP_LINES: tuple[tuple[str, str], ...] = (
    ("clear-display-name", "Drop the saved friendly label for a device (SQLite cache required)."),
    ("close-door", "Tell Tailwind to fully close a door (match name, index, or id)."),
    (
        "discover-androidtv",
        "Cast mDNS browse (PyChromecast); optional seconds timeout; cache when SQLite on.",
    ),
    (
        "discover-ep1",
        "ESPHome mDNS browse for Everything Presence One; optional seconds timeout.",
    ),
    ("edit-mode", "Switch Emacs vs Vim keys for this session: edit-mode emacs | vim."),
    ("exit", "Leave the REPL."),
    ("help", "Show this list."),
    ("is-on", "Print whether a Kasa switch or Cast target is on (media playing) or off."),
    ("is-open", "Print whether a Tailwind door reads fully open."),
    (
        "kasa-creds",
        "Prompt for Kasa/Tapo account email + password (password hidden), "
        "persist to encrypted storage when configured, and rediscover.",
    ),
    ("open-door", "Tell Tailwind to fully open a door."),
    ("pause", "Pause playback on a Sonos speaker."),
    ("quit", "Leave the REPL (same as exit)."),
    (
        "read-ep1",
        "Live-read Everything Presence One occupancy / climate / lux (optional name, MAC, or id).",
    ),
    ("refresh", "Reconnect all backends; Kasa may reuse cached discovery."),
    ("refresh-discovery", "Full LAN discovery: Google Cast, EP1, Kasa, Sonos, Tailwind, Vizio."),
    ("resume", "Resume playback on a Sonos speaker."),
    ("set-display-name", "Save a friendly label for a device (SQLite cache required)."),
    (
        "setup-secrets",
        "Create or update domesti-bot.config.json (Fernet key for encrypted DB secrets).",
    ),
    ("show-devices", "List Google Cast, Kasa, Sonos, Tailwind, EP1, and Vizio devices."),
    ("turn-off", "Turn a Kasa switch off, or stop media on a Cast target."),
    ("turn-on", "Turn a Kasa switch on, or resume paused Cast media if applicable."),
)

_EDIT_MODE_SUBARGS: tuple[str, ...] = ("emacs", "vim")


class _Theme:
    """ANSI styling for stdout/stderr when coloring is enabled (TTY + ``NO_COLOR`` + ``--color``)."""

    __slots__ = ("_enabled",)

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    def _s(self, codes: str, text: str) -> str:
        if not self._enabled:
            return text
        return f"\033[{codes}m{text}\033[0m"

    def cmd(self, text: str) -> str:
        return self._s("33;1", text)

    def completion_command_style(self) -> str:
        return "bold ansiyellow" if self._enabled else ""

    def completion_parameter_style(self) -> str:
        return "bold ansibrightmagenta" if self._enabled else ""

    def device(self, text: str) -> str:
        return self._s("35;1", text)

    def dim(self, text: str) -> str:
        return self._s("2", text)

    def err(self, text: str) -> str:
        return self._s("31", text)

    def header(self, text: str) -> str:
        return self._s("34;1", text)

    def meta(self, text: str) -> str:
        return self._s("90", text)

    def ok(self, text: str) -> str:
        return self._s("32", text)

    def state(self, text: str) -> str:
        return self._s("36", text)

    def warn(self, text: str) -> str:
        return self._s("33", text)


# Lexicographic order by slug (matches lex order of display names: Google Cast, GoTailwind, Kasa, Sonos).
_FAMILY_BOOT_SLUGS: tuple[str, ...] = (
    "androidtv",
    "ep1",
    "gotailwind",
    "kasa",
    "sonos",
    "vizio",
)
_FAMILY_BOOT_LABEL: dict[str, str] = {
    "androidtv": "Google Cast",
    "ep1": "Everything Presence One",
    "gotailwind": "GoTailwind",
    "kasa": "Kasa",
    "sonos": "Sonos",
    "vizio": "Vizio",
}
# Plural unit name used in the per-backend "ready" line. Singular forms are not
# needed because the count is shown as a bare integer (``"0 speakers"``, ``"1 speakers"``).
_FAMILY_UNIT_PLURAL: dict[str, str] = {
    "androidtv": "devices",
    "ep1": "sensors",
    "gotailwind": "doors",
    "kasa": "switches",
    "sonos": "speakers",
    "vizio": "TVs",
}
# Human-friendly label for the ``last_discovery_source`` signal each backend
# attaches to its boot bundle. ``None`` (e.g. Tailwind, which has no LAN
# discovery) prints no source annotation at all.
_FAMILY_SOURCE_LABEL: dict[str, str] = {
    "cache": "cache",
    "discovery": "LAN discovery",
}

# Commands that need a family to be past PENDING. ``show-devices`` is omitted —
# it renders a per-family discovering hint instead of blocking the whole listing.
_DEVICE_COMMAND_FAMILIES: dict[str, tuple[str, ...]] = {
    "clear-display-name": ("androidtv", "gotailwind", "kasa"),
    "close-door": ("gotailwind",),
    "discover-androidtv": ("androidtv",),
    "discover-ep1": ("ep1",),
    "is-on": ("androidtv", "kasa"),
    "is-open": ("gotailwind",),
    "kasa-creds": ("kasa",),
    "open-door": ("gotailwind",),
    "pause": ("sonos",),
    "read-ep1": ("ep1",),
    "refresh": ("androidtv", "ep1", "gotailwind", "kasa", "sonos", "vizio"),
    "refresh-discovery": ("androidtv", "ep1", "gotailwind", "kasa", "sonos", "vizio"),
    "resume": ("sonos",),
    "set-display-name": ("androidtv", "gotailwind", "kasa"),
    "turn-off": ("androidtv", "kasa"),
    "turn-on": ("androidtv", "kasa"),
}

_SLUG_TO_MGR_ATTR: dict[str, str] = {
    "androidtv": "androidtv_mgr",
    "ep1": "ep1_mgr",
    "gotailwind": "tailwind_mgr",
    "sonos": "sonos_mgr",
    "vizio": "vizio_mgr",
}


class FamilyDiscoveryStatus(StrEnum):
    """Per-family bootstrap state for the local CLI (not an HTTP wire enum)."""

    FAILED = "failed"
    PENDING = "pending"
    READY = "ready"
    SKIPPED = "skipped"


def _parse_ep1_host_specs(cli_hosts: list[str]) -> list[tuple[str, int]]:
    """Merge ``--ep1-host`` with ``EP1_HOSTS`` into ``(host, port)`` pairs."""
    specs: list[str] = list(cli_hosts)
    env = (os.environ.get("EP1_HOSTS") or "").strip()
    if env:
        specs.extend(part.strip() for part in env.split(",") if part.strip())
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for raw in specs:
        host, port = _split_host_port(raw, DEFAULT_EP1_API_PORT)
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _split_host_port(spec: str, default_port: int) -> tuple[str, int]:
    text = spec.strip()
    if not text:
        raise ValueError(f"Expected HOST or HOST:PORT, got {spec!r}")
    if text.count(":") != 1:
        return text, default_port
    host, port_s = text.rsplit(":", 1)
    host = host.strip()
    port_s = port_s.strip()
    if not host:
        raise ValueError(f"Expected HOST or HOST:PORT, got {spec!r}")
    if not port_s:
        raise ValueError(f"Expected HOST:PORT with a numeric port, got empty port in {spec!r}")
    if not port_s.isdigit():
        raise ValueError(f"Expected HOST:PORT with a numeric port, got {spec!r}")
    return host, int(port_s)


def _bootstrap_family_summary(
    slug: str,
    result: dict[str, Any],
    *,
    ok_verb: str,
) -> str:
    """Format one backend's bootstrap outcome for structured log output."""
    label = _FAMILY_BOOT_LABEL[slug]
    prefix = f"[startup] {label}:"
    if result.get("skipped"):
        detail = (result.get("detail") or "").strip()
        suffix = f" — {detail}" if detail else ""
        return f"{prefix} skipped{suffix}"
    if result.get("exc") is not None:
        return f"{prefix} failed — {result['exc']}"
    if result.get("ok"):
        source_label = _FAMILY_SOURCE_LABEL.get(str(result.get("source") or ""))
        count = result.get("count")
        unit = _FAMILY_UNIT_PLURAL.get(slug)
        bits: list[str] = []
        if source_label is not None:
            bits.append(source_label)
        if count is not None and unit is not None:
            bits.append(f"{count} {unit}")
        suffix = f" ({', '.join(bits)})" if bits else ""
        return f"{prefix} {ok_verb}{suffix}"
    return f"{prefix} (no status)"


def _print_family_parallel_line(
    theme: _Theme,
    slug: str,
    result: dict[str, Any],
    *,
    ok_verb: str,
) -> None:
    label = _FAMILY_BOOT_LABEL[slug]
    if result.get("skipped"):
        detail = (result.get("detail") or "").strip()
        suffix = f" — {detail}" if detail else ""
        print(theme.dim(f"  {label}: skipped{suffix}"), flush=True)
    elif result.get("exc") is not None:
        ex = result["exc"]
        print(theme.err(f"  {label}: failed — {ex}"), file=sys.stderr, flush=True)
    elif result.get("ok"):
        source_label = _FAMILY_SOURCE_LABEL.get(str(result.get("source") or ""))
        count = result.get("count")
        unit = _FAMILY_UNIT_PLURAL.get(slug)
        # Compose ``ready (<source>, N <unit>)`` when we have both signals;
        # fall back to bare ``ready`` for backends that don't report them.
        bits: list[str] = []
        if source_label is not None:
            bits.append(source_label)
        if count is not None and unit is not None:
            bits.append(f"{count} {unit}")
        suffix = f" ({', '.join(bits)})" if bits else ""
        print(theme.ok(f"  {label}: {ok_verb}{suffix}"), flush=True)
    else:
        print(theme.dim(f"  {label}: (no status)"), flush=True)


async def _timed_family_boot(
    slug: str,
    boot_coro: Awaitable[dict[str, Any]],
    *,
    log_progress: bool,
    theme: _Theme | None = None,
    session: _CliDiscoverySession | None = None,
) -> dict[str, Any]:
    """Run one backend ``fetch`` and log start/finish with wall-clock timing."""
    if log_progress:
        _LOGGER.info(
            "[startup] %s: discovery starting",
            _FAMILY_BOOT_LABEL[slug],
        )
    started = time.monotonic()
    result = await boot_coro
    if session is not None:
        session.apply_family_result(slug, result)
    if log_progress:
        _LOGGER.info(
            "%s in %.1fs",
            _bootstrap_family_summary(slug, result, ok_verb="ready"),
            time.monotonic() - started,
        )
        if theme is not None:
            _print_family_parallel_line(theme, slug, result, ok_verb="ready")
            if slug == "kasa" and result.get("ok") and session is not None:
                _maybe_print_kasa_auth_notice(session.kasa_mgr, theme=theme)
    return result


class _CmdCtx(NamedTuple):
    """Completing the first token (hyphenated command)."""

    partial: str


class _ArgCtx(NamedTuple):
    """Completing device alias after a full command."""

    command: str
    arg_prefix: str


def _normalize_edit_mode_choice(raw: str | None) -> str:
    """Normalize ``DEVICE_MANAGER_EDIT_MODE`` (or similar) to ``emacs`` or ``vim``.

    When unset or blank, defaults to **vim**. Explicit ``emacs`` (or ``e``) selects Emacs;
    ``vim`` / ``vi`` / ``v`` select Vim. Any other non-empty value falls back to vim.
    """

    if raw is None:
        return "vim"
    s = str(raw).strip().lower()
    if not s:
        return "vim"
    if s in ("emacs", "e"):
        return "emacs"
    if s in ("vi", "vim", "v"):
        return "vim"
    return "vim"


def _parse_completion_buffer(text_before_cursor: str) -> _CmdCtx | _ArgCtx | None:
    """Classify the line fragment left of the cursor for tab-completion."""
    raw = text_before_cursor
    t = raw.lstrip(" \t")

    if not t.strip():
        return _CmdCtx(partial="")

    for cmd in sorted(COMMANDS, key=len, reverse=True):
        if t.startswith(cmd + " ") or t == cmd:
            arg_prefix = t[len(cmd) :].lstrip(" \t")
            return _ArgCtx(command=cmd, arg_prefix=arg_prefix)

    if not any(c in t for c in " \t"):
        return _CmdCtx(partial=t)

    first = t.split(None, 1)[0]
    if first in COMMANDS:
        rest = t[len(first) :].lstrip(" \t")
        return _ArgCtx(command=first, arg_prefix=rest)
    return _CmdCtx(partial=first)


def _print_help(theme: _Theme) -> None:
    print(theme.header("Commands"))
    cmd_width = max(len(name) for name, _ in _COMMAND_HELP_LINES)
    for name, blurb in _COMMAND_HELP_LINES:
        gap = " " * (cmd_width - len(name) + 2)
        print(f"  {theme.cmd(name)}{gap}{theme.dim(blurb)}")
    print()
    print(
        theme.dim(
            "Tip: names match prefixes and ignore case; Tab completes commands and devices. "
            "Switch targets: Kasa + Google Cast share turn-on/off/is-on. Cast: PyChromecast mDNS "
            "by default; disable with --no-androidtv-zeroconf or ANDROIDTV_ZEROCONF=0. "
            "discover-androidtv in the REPL for an explicit browse. "
            "Sonos: pause/resume only (--no-sonos skips UDP discovery). "
            "Startup line-editing: --edit-mode or DEVICE_MANAGER_EDIT_MODE (default vim)."
        )
    )


def split_invocation(line: str) -> tuple[str, str] | None:
    """Split ``line`` into ``(command, argument_rest)`` or ``None`` if unknown."""
    s = line.strip()
    if not s:
        return None
    for cmd in sorted(COMMANDS, key=len, reverse=True):
        if s == cmd:
            return cmd, ""
        if s.startswith(cmd + " "):
            return cmd, s[len(cmd) + 1 :].lstrip()
    return None


def _kasa_switch_aliases(mgr: KasaDeviceManager) -> list[str]:
    return [item.display for item in _kasa_switch_completion_items(mgr)]


def _kasa_switch_completion_items(mgr: KasaDeviceManager) -> list[CompletionAlias]:
    try:
        items = [device_completion_alias(s.identifier, s.preferred_label) for s in mgr.switches]
        return sorted(items, key=lambda item: item.display.lower())
    except NotInitializedError:
        return []


def _kasa_switch_count(mgr: KasaDeviceManager) -> int:
    try:
        return len(mgr.switches)
    except NotInitializedError:
        return 0


def _maybe_print_kasa_auth_notice(kasa_mgr: KasaDeviceManager, *, theme: _Theme) -> None:
    """One-shot suggestion when KLAP devices were skipped over auth.

    Fires after the ``Ready`` banner when (a) at least one device was
    skipped because ``Discover.discover`` came back with KLAP that
    needed account creds we didn't have, and (b) we don't already have
    creds configured (those failures are a different problem covered by
    the per-device WARNING from ``kasa_device_manager``). The notice
    points at the ``kasa-creds`` REPL command for a no-restart fix.
    """

    skipped = kasa_mgr.skipped_auth_hosts
    if not skipped or kasa_mgr.has_credentials:
        return
    n = len(skipped)
    sample = ", ".join(skipped[:3]) + (", …" if n > 3 else "")
    print(
        f"{theme.warn(f'Notice: {n} Kasa device(s) need account credentials for the KLAP handshake')} "
        f"{theme.dim(f'({sample})')}"
    )
    print(
        f"  {theme.dim('Type')} {theme.cmd('kasa-creds')} "
        f"{theme.dim('to enter your Kasa/Tapo email/password (hidden) and rediscover,')}"
    )
    print(f"  {theme.dim('or open Settings → Kasa, or set KASA_USERNAME + KASA_PASSWORD before restart.')}")


async def _maybe_restart_device_state_watchers_after_ep1() -> None:
    """Hot-reload watchers when EP1 rediscover runs under the HTTP server.

    No-op in the standalone REPL (``runtime.device_state`` is unset). Required
    after ``rediscover`` because ``Ep1SubscriptionWatcher`` holds the previous
    ``Ep1Device`` instances from startup.

    ``server_runtime`` is imported inside this helper (not at module top level)
    because a top-level import creates a cycle:
    ``domesti_bot_cli`` → ``server_runtime`` → ``device_state_watcher`` →
    ``domesti_bot_cli``.
    """

    # Circular-import break: see docstring (AGENTS.md module-level import rule).
    from app.server_runtime import runtime

    if runtime.device_state is None:
        return
    try:
        await runtime.restart_device_state_watchers()
    except Exception:
        _LOGGER.warning("EP1 watcher restart after rediscover failed", exc_info=True)


async def _repl_cmd_kasa_creds(
    kasa_mgr: KasaDeviceManager,
    *,
    prompt_fn: Callable[[str, bool], Awaitable[str]],
    theme: _Theme,
    cache_path: Path | None = None,
) -> None:
    """Interactive Kasa credential entry + rediscover (driven by ``prompt_fn``).

    ``prompt_fn(message, is_password)`` is the injection point: the
    REPL wires it to a fresh :class:`prompt_toolkit.PromptSession`'s
    ``prompt_async`` so the password field is starred. Tests pass a
    canned-answer function so this helper stays exercisable without
    prompt_toolkit's terminal layer.

    When ``cache_path`` is set and a Fernet secrets key is configured,
    credentials are also written to encrypted ``app_secrets`` so they
    survive process restart (same store as Settings → Kasa).
    """

    print(
        f"{theme.header('Kasa credentials')} "
        f"{theme.dim('(password hidden — persisted to encrypted storage when secrets key is configured)')}"
    )
    try:
        username = await prompt_fn("  Kasa account email: ", False)
        password = await prompt_fn("  Kasa password: ", True)
    except (EOFError, KeyboardInterrupt):
        print(theme.err("kasa-creds: cancelled"), file=sys.stderr)
        return
    try:
        kasa_mgr.set_credentials(username=username, password=password)
    except ValueError as ex:
        print(theme.err(f"kasa-creds: {ex}"), file=sys.stderr)
        return
    if cache_path is not None:
        try:
            save_kasa_credentials_to_db(
                cache_path,
                username=username,
                password=password,
            )
            print(theme.dim("kasa-creds: saved to encrypted discovery database"))
        except SecretsConfigurationError as ex:
            print(theme.warn(f"kasa-creds: not persisted ({ex})"))
            print(
                f"  {theme.dim('Run')} {theme.cmd('setup-secrets')} "
                f"{theme.dim('or set DOMESTI_BOT_SECRETS_KEY to persist across restarts.')}"
            )
        except ValueError as ex:
            print(theme.err(f"kasa-creds: {ex}"), file=sys.stderr)
            return
    else:
        print(theme.warn("kasa-creds: no discovery cache — credentials are in-memory only"))
    print(theme.dim("kasa-creds: rediscovering Kasa devices…"))
    try:
        await kasa_mgr.rediscover()
    except Exception as ex:
        print(theme.err(f"kasa-creds: rediscover failed: {ex}"), file=sys.stderr)
        return
    n_switches = _kasa_switch_count(kasa_mgr)
    skipped = kasa_mgr.skipped_auth_hosts
    if skipped:
        print(theme.warn(f"kasa-creds: {len(skipped)} device(s) still failed auth: {', '.join(skipped)}"))
        print(f"  {theme.dim('Likely a wrong account email/password.')}")
    print(theme.ok(f"Kasa: ready ({n_switches} switch(es))"))


async def _repl_cmd_setup_secrets(
    *,
    prompt_fn: Callable[[str, bool], Awaitable[str]],
    theme: _Theme,
) -> None:
    """Create or update ``domesti-bot.config.json`` (driven by ``prompt_fn`` for tests)."""

    path = secrets_json_path()
    print(f"{theme.header('Secrets file')} {theme.dim(f'({path})')}")
    if (os.environ.get("DOMESTI_BOT_SECRETS_KEY") or "").strip():
        print(theme.warn("DOMESTI_BOT_SECRETS_KEY is set in the environment and overrides the JSON file."))
    if path.is_file():
        try:
            overwrite = await prompt_fn(
                "  domesti-bot.config.json already exists. Overwrite? [y/N]: ",
                False,
            )
        except (EOFError, KeyboardInterrupt):
            print(theme.err("setup-secrets: cancelled"), file=sys.stderr)
            return
        if overwrite.strip().lower() not in {"y", "yes"}:
            print(theme.dim("setup-secrets: left existing file unchanged"))
            return
    try:
        generate_answer = await prompt_fn("  Generate new Fernet key? [Y/n]: ", False)
    except (EOFError, KeyboardInterrupt):
        print(theme.err("setup-secrets: cancelled"), file=sys.stderr)
        return
    if generate_answer.strip().lower() in {"", "y", "yes"}:
        key = generate_fernet_key()
        print(theme.dim("setup-secrets: generated a new Fernet key"))
    else:
        try:
            key = await prompt_fn("  Paste Fernet key (hidden): ", True)
        except (EOFError, KeyboardInterrupt):
            print(theme.err("setup-secrets: cancelled"), file=sys.stderr)
            return
    try:
        written = write_secrets_json(key, path=path)
    except ValueError as ex:
        print(theme.err(f"setup-secrets: {ex}"), file=sys.stderr)
        return
    print(theme.ok(f"Wrote {written} (mode 0600, gitignored)"))
    print(
        theme.dim(
            "Restart domesti-bot-server (or redeploy) so new processes pick up the file. "
            "Use the web UI Settings menu or setup-secrets to store Tailwind tokens in SQLite."
        )
    )


def _all_cli_device_completion_items(
    kasa_mgr: KasaDeviceManager,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None = None,
) -> list[CompletionAlias]:
    items = list(_androidtv_switch_completion_items(androidtv_mgr))
    items.extend(_kasa_switch_completion_items(kasa_mgr))
    if tailwind_mgr is not None:
        items.extend(_tailwind_door_completion_items(tailwind_mgr))
    return sorted(items, key=lambda item: item.display.lower())


def _all_cli_device_labels(
    kasa_mgr: KasaDeviceManager,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None = None,
) -> list[str]:
    return [item.display for item in _all_cli_device_completion_items(kasa_mgr, tailwind_mgr, androidtv_mgr)]


def _append_device_label_triples(
    triples: list[tuple[str, str, str]],
    *,
    backend: str,
    api_id: str,
    identifier: str,
    preferred_label: str,
    extra: tuple[str, ...] = (),
) -> None:
    """Register MAC, preferred label, extras, and ``Name (mac)`` as resolve keys."""

    display = format_device_display(identifier, preferred_label)
    seen: set[str] = set()
    for raw in (identifier, preferred_label, *extra, display):
        label = (raw or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        triples.append((label, backend, api_id))


def _collect_label_triples(
    kasa_mgr: KasaDeviceManager,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None = None,
) -> list[tuple[str, str, str]]:
    """``(surface_label, backend, api_lookup_id)`` — Android TV, Kasa, Tailwind (lex family order)."""

    triples: list[tuple[str, str, str]] = []
    if androidtv_mgr is not None:
        try:
            for d in androidtv_mgr.switches:
                _append_device_label_triples(
                    triples,
                    backend="androidtv",
                    api_id=d.identifier,
                    identifier=d.identifier,
                    preferred_label=d.preferred_label,
                )
        except NotInitializedError:
            pass
    try:
        for kd in kasa_mgr.switches:
            _append_device_label_triples(
                triples,
                backend="kasa",
                api_id=kd.identifier,
                identifier=kd.identifier,
                preferred_label=kd.preferred_label,
            )
    except NotInitializedError:
        pass
    if tailwind_mgr is not None:
        try:
            for gd in tailwind_mgr.doors:
                _append_device_label_triples(
                    triples,
                    backend="tailwind",
                    api_id=gd.identifier,
                    identifier=gd.identifier,
                    preferred_label=gd.preferred_label,
                    extra=(str(gd.door_index),),
                )
        except NotInitializedError:
            pass
    return triples


def _editing_mode_enum(mode: str) -> EditingMode:
    return EditingMode.VI if mode == "vim" else EditingMode.EMACS


def _ep1_readings_summary(device: Ep1Device) -> str:
    return (
        f"occupancy={device.occupancy_state} "
        f"temp_c={device.temperature_c} "
        f"humidity_pct={device.humidity_pct} "
        f"illuminance_lx={device.illuminance_lx}"
    )


def _ep1_sensor_count(mgr: Ep1DeviceManager | None) -> int:
    if mgr is None:
        return 0
    try:
        return len(mgr.devices)
    except NotInitializedError:
        return 0


def _preferred_surface_label(labels: Sequence[str]) -> str:
    """Pick the human ``Name (mac)`` surface when several labels map to one device."""

    for lab in labels:
        if " (" in lab and lab.endswith(")"):
            return lab
    return sorted(labels, key=len, reverse=True)[0]


def _resolve_cli_target(
    raw: str,
    triples: list[tuple[str, str, str]],
) -> tuple[str | None, list[str], tuple[str, str] | None]:
    """Return ``(api_lookup_id, ambiguous_labels, (backend, api_id))``.

    Multiple surface labels for the same device (MAC, preferred label,
    ``Name (mac)``) count as one match so prefix resolution is not falsely
    ambiguous.
    """

    q = raw.strip()
    if not q:
        return None, [], None
    lower_q = q.lower()

    def unique_targets(selected: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        by_key: dict[tuple[str, str], list[str]] = {}
        order: list[tuple[str, str]] = []
        for lab, backend, api in selected:
            key = (backend, api)
            if key not in by_key:
                by_key[key] = []
                order.append(key)
            by_key[key].append(lab)
        out: list[tuple[str, str, str]] = []
        for backend, api in order:
            out.append((_preferred_surface_label(by_key[(backend, api)]), backend, api))
        return out

    exact = unique_targets([t for t in triples if t[0].lower() == lower_q])
    if len(exact) == 1:
        _lab, backend, api = exact[0]
        return api, [], (backend, api)
    if len(exact) > 1:
        return None, sorted({t[0] for t in exact}), None

    prefix_hits = unique_targets([t for t in triples if t[0].lower().startswith(lower_q)])
    if len(prefix_hits) == 1:
        _lab, backend, api = prefix_hits[0]
        return api, [], (backend, api)
    if len(prefix_hits) > 1:
        return None, sorted({t[0] for t in prefix_hits}), None
    return None, [], None


def _greedy_resolve_set_display_tokens(
    tokens: list[str],
    triples: list[tuple[str, str, str]],
) -> tuple[tuple[str, str], str] | None:
    if len(tokens) < 2:
        return None
    for i in range(len(tokens) - 1, 0, -1):
        frag = " ".join(tokens[:i])
        api_id, amb, meta = _resolve_cli_target(frag, triples)
        if api_id is not None and not amb and meta is not None:
            rest = " ".join(tokens[i:]).strip()
            if rest:
                return (meta[0], meta[1]), rest
    return None


def _sqlite_canonical_key(
    backend: str,
    api_lookup_id: str,
    kasa_mgr: KasaDeviceManager,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None = None,
) -> str | None:
    if backend == "kasa":
        kd = kasa_mgr.get_device_by_alias(api_lookup_id)
        return kd.host if kd is not None else None
    if backend == "tailwind" and tailwind_mgr is not None:
        gd = tailwind_mgr.get_device_by_alias(api_lookup_id)
        return gd.identifier if gd is not None else None
    if backend == "androidtv" and androidtv_mgr is not None:
        dev = androidtv_mgr.get_device_by_alias(api_lookup_id)
        return dev.identifier if dev is not None else None
    return None


def _sonos_zone_aliases(mgr: SonosDeviceManager | None) -> list[str]:
    return [item.display for item in _sonos_zone_completion_items(mgr)]


def _sonos_zone_completion_items(mgr: SonosDeviceManager | None) -> list[CompletionAlias]:
    if mgr is None:
        return []
    try:
        items = [device_completion_alias(p.identifier, p.preferred_label) for p in mgr.players]
        return sorted(items, key=lambda item: item.display.lower())
    except NotInitializedError:
        return []


def _sonos_zone_count(mgr: SonosDeviceManager | None) -> int:
    if mgr is None:
        return 0
    try:
        return len(mgr.players)
    except NotInitializedError:
        return 0


def _vizio_has_any_auth(
    cache_path: Path | None,
    *,
    cli_token: str | None,
    env_token: str | None,
) -> bool:
    if (cli_token or env_token or os.environ.get("VIZIO_AUTH_TOKEN") or "").strip():
        return True
    if cache_path is None:
        return False
    from app.db.secrets import (
        load_vizio_auth_hosts_from_db,
        load_vizio_auth_token_from_db,
    )

    if load_vizio_auth_hosts_from_db(cache_path):
        return True
    for host, port, *_rest, mac, _diid in device_discovery_store.load_vizio_tvs(cache_path):
        if load_vizio_auth_token_from_db(cache_path, mac=mac, host=host):
            return True
    return False


def _vizio_targets_available(
    cache_path: Path | None,
    configured_hosts: list[tuple[str, int]],
) -> bool:
    if configured_hosts:
        return True
    if cache_path is None:
        return False
    from app.db.secrets import load_vizio_auth_hosts_from_db

    if load_vizio_auth_hosts_from_db(cache_path):
        return True
    return bool(device_discovery_store.load_vizio_tvs(cache_path))


def _vizio_tv_count(mgr: VizioDeviceManager | None) -> int:
    if mgr is None:
        return 0
    try:
        return len(mgr.tvs)
    except NotInitializedError:
        return 0


def _androidtv_switch_aliases(mgr: AndroidTvDeviceManager | None) -> list[str]:
    return [item.display for item in _androidtv_switch_completion_items(mgr)]


def _androidtv_switch_completion_items(mgr: AndroidTvDeviceManager | None) -> list[CompletionAlias]:
    if mgr is None:
        return []
    try:
        items = [device_completion_alias(d.identifier, d.preferred_label) for d in mgr.switches]
        return sorted(items, key=lambda item: item.display.lower())
    except NotInitializedError:
        return []


def _androidtv_switch_count(mgr: AndroidTvDeviceManager | None) -> int:
    if mgr is None:
        return 0
    try:
        return len(mgr.switches)
    except NotInitializedError:
        return 0


def _collect_ep1_triples(ep1_mgr: Ep1DeviceManager | None) -> list[tuple[str, str, str]]:
    triples: list[tuple[str, str, str]] = []
    if ep1_mgr is None:
        return triples
    try:
        for device in ep1_mgr.devices:
            extra: tuple[str, ...] = ()
            if device.mac_address and device.mac_address != device.identifier:
                extra = (device.mac_address,)
            _append_device_label_triples(
                triples,
                backend="ep1",
                api_id=device.identifier,
                identifier=device.identifier,
                preferred_label=device.preferred_label,
                extra=extra,
            )
    except NotInitializedError:
        pass
    return triples


def _collect_media_triples(sonos_mgr: SonosDeviceManager | None) -> list[tuple[str, str, str]]:
    triples: list[tuple[str, str, str]] = []
    if sonos_mgr is not None:
        try:
            for p in sonos_mgr.players:
                _append_device_label_triples(
                    triples,
                    backend="sonos",
                    api_id=p.identifier,
                    identifier=p.identifier,
                    preferred_label=p.preferred_label,
                )
        except NotInitializedError:
            pass
    return triples


def _collect_switch_triples(
    kasa_mgr: KasaDeviceManager,
    androidtv_mgr: AndroidTvDeviceManager | None,
) -> list[tuple[str, str, str]]:
    triples: list[tuple[str, str, str]] = []
    if androidtv_mgr is not None:
        try:
            for d in androidtv_mgr.switches:
                _append_device_label_triples(
                    triples,
                    backend="androidtv",
                    api_id=d.identifier,
                    identifier=d.identifier,
                    preferred_label=d.preferred_label,
                )
        except NotInitializedError:
            pass
    try:
        for kd in kasa_mgr.switches:
            _append_device_label_triples(
                triples,
                backend="kasa",
                api_id=kd.identifier,
                identifier=kd.identifier,
                preferred_label=kd.preferred_label,
            )
    except NotInitializedError:
        pass
    return triples


def _media_playback_aliases(sonos_mgr: SonosDeviceManager | None) -> list[str]:
    return [item.display for item in _sonos_zone_completion_items(sonos_mgr)]


def _media_playback_completion_items(sonos_mgr: SonosDeviceManager | None) -> list[CompletionAlias]:
    return list(_sonos_zone_completion_items(sonos_mgr))


def _switch_aliases(
    kasa_mgr: KasaDeviceManager,
    androidtv_mgr: AndroidTvDeviceManager | None,
) -> list[str]:
    return [item.display for item in _switch_completion_items(kasa_mgr, androidtv_mgr)]


def _switch_completion_items(
    kasa_mgr: KasaDeviceManager,
    androidtv_mgr: AndroidTvDeviceManager | None,
) -> list[CompletionAlias]:
    items = list(_androidtv_switch_completion_items(androidtv_mgr))
    items.extend(_kasa_switch_completion_items(kasa_mgr))
    return sorted(items, key=lambda item: item.display.lower())


def _resolve_device_name(raw: str, candidates: list[str]) -> tuple[str | None, list[str]]:
    """Resolve user input to a canonical id.

    Returns ``(canonical, [])`` on success, or ``(None, [])`` if nothing matched,
    or ``(None, ambiguous_names)`` if several candidates matched the same prefix.
    Matching is case-insensitive for both exact and prefix rules.
    """
    q = raw.strip()
    if not q:
        return None, []

    lower_q = q.lower()
    exact = [c for c in candidates if c.lower() == lower_q]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, sorted(set(exact))

    prefix_hits = [c for c in candidates if c.lower().startswith(lower_q)]
    if len(prefix_hits) == 1:
        return prefix_hits[0], []
    if len(prefix_hits) > 1:
        return None, sorted(set(prefix_hits))
    return None, []


def _repl_prompt_message(theme: _Theme) -> HTML | str:
    if not theme._enabled:
        return "device_manager> "
    return HTML('<style fg="ansicyan"><b>device_manager</b></style><style fg="ansibrightblack"> &gt; </style>')


def _report_resolve_failure(theme: _Theme, kind: str, arg: str, ambiguous: list[str]) -> None:
    if ambiguous:
        opts = ", ".join(repr(x) for x in ambiguous)
        msg = f"Ambiguous {kind} {arg!r}; try: {opts}"
    else:
        msg = f"No {kind} matches {arg!r} (case-insensitive)."
    print(theme.err(msg), file=sys.stderr)


def _stdout_color_enabled(mode: str) -> bool:
    if (os.environ.get("NO_COLOR") or "").strip():
        return False
    if mode == "never":
        return False
    if mode == "always":
        return True
    return sys.stdout.isatty()


def _tailwind_door_aliases(mgr: GotailwindDeviceManager | None) -> list[str]:
    return [item.display for item in _tailwind_door_completion_items(mgr)]


def _tailwind_door_completion_items(mgr: GotailwindDeviceManager | None) -> list[CompletionAlias]:
    if mgr is None:
        return []
    try:
        items = [device_completion_alias(d.identifier, d.preferred_label, str(d.door_index)) for d in mgr.doors]
        return sorted(items, key=lambda item: item.display.lower())
    except NotInitializedError:
        return []


def _tailwind_door_count(mgr: GotailwindDeviceManager | None) -> int:
    if mgr is None:
        return 0
    try:
        return len(mgr.doors)
    except NotInitializedError:
        return 0


class _RemoteAliasBundles(NamedTuple):
    switch: list[CompletionAlias]
    sonos: list[CompletionAlias]
    tailwind: list[CompletionAlias]
    all_device_labels: list[CompletionAlias]


def _completion_aliases_pending(
    discovery: _CliDiscoverySession | None,
    *slugs: str,
) -> bool:
    return discovery is not None and discovery.families_pending(*slugs)


def _yield_alias_completions(
    *,
    items: list[CompletionAlias],
    prefix: str,
    style: str,
    pending: bool,
) -> Iterator[Completion]:
    if pending:
        yield Completion(
            prefix,
            start_position=-len(prefix),
            display=COMPLETION_DISCOVERING_HINT,
        )
        return
    prefix_lower = prefix.lower()
    for item in items:
        if completion_alias_matches(item, prefix_lower):
            yield Completion(item.display, start_position=-len(prefix), style=style)


class _ReplCompleterRemote(Completer):
    def __init__(self, *, bundles: _RemoteAliasBundles, theme: _Theme) -> None:
        self._bundles = bundles
        self._theme = theme

    def get_completions(self, document, complete_event):  # noqa: ANN001
        buf = document.text_before_cursor
        ctx = _parse_completion_buffer(buf)
        if ctx is None:
            return

        if isinstance(ctx, _CmdCtx):
            partial = ctx.partial
            st = self._theme.completion_command_style()
            for cmd in COMMANDS:
                if cmd.startswith(partial):
                    yield Completion(cmd, start_position=-len(partial), style=st)
            return

        if ctx.command in (
            "discover-androidtv",
            "discover-ep1",
            "exit",
            "quit",
            "help",
            "read-ep1",
            "show-devices",
            "refresh",
            "refresh-discovery",
        ):
            return

        prefix = ctx.arg_prefix
        st = self._theme.completion_parameter_style()
        pending = False
        if ctx.command in ("turn-on", "turn-off", "is-on"):
            aliases = self._bundles.switch
        elif ctx.command in ("pause", "resume"):
            aliases = self._bundles.sonos
        elif ctx.command in ("open-door", "close-door", "is-open"):
            aliases = self._bundles.tailwind
        elif ctx.command in ("set-display-name", "clear-display-name"):
            aliases = self._bundles.all_device_labels
        elif ctx.command == "edit-mode":
            aliases = [CompletionAlias(display=name, matches=()) for name in _EDIT_MODE_SUBARGS]
        else:
            return

        yield from _yield_alias_completions(
            items=aliases,
            prefix=prefix,
            style=st,
            pending=pending,
        )


class _ReplCompleter(Completer):
    def __init__(
        self,
        *,
        androidtv: AndroidTvDeviceManager | None,
        kasa: KasaDeviceManager,
        sonos: SonosDeviceManager | None,
        tailwind: GotailwindDeviceManager | None,
        theme: _Theme,
        discovery: _CliDiscoverySession | None = None,
    ) -> None:
        self._androidtv = androidtv
        self._kasa = kasa
        self._sonos = sonos
        self._tailwind = tailwind
        self._theme = theme
        self._discovery = discovery

    def get_completions(self, document, complete_event):  # noqa: ANN001
        buf = document.text_before_cursor
        ctx = _parse_completion_buffer(buf)
        if ctx is None:
            return

        if isinstance(ctx, _CmdCtx):
            partial = ctx.partial
            st = self._theme.completion_command_style()
            for cmd in COMMANDS:
                if cmd.startswith(partial):
                    yield Completion(cmd, start_position=-len(partial), style=st)
            return

        if ctx.command in (
            "discover-androidtv",
            "discover-ep1",
            "exit",
            "quit",
            "help",
            "read-ep1",
            "show-devices",
            "refresh",
            "refresh-discovery",
        ):
            return

        prefix = ctx.arg_prefix
        st = self._theme.completion_parameter_style()
        androidtv, kasa, sonos, tailwind = self._live_managers()
        if ctx.command in ("turn-on", "turn-off", "is-on"):
            items = _switch_completion_items(kasa, androidtv)
            pending = _completion_aliases_pending(self._discovery, "androidtv", "kasa")
        elif ctx.command in ("pause", "resume"):
            items = _media_playback_completion_items(sonos)
            pending = _completion_aliases_pending(self._discovery, "sonos")
        elif ctx.command in ("open-door", "close-door", "is-open"):
            items = _tailwind_door_completion_items(tailwind)
            pending = _completion_aliases_pending(self._discovery, "gotailwind")
        elif ctx.command in ("set-display-name", "clear-display-name"):
            items = _all_cli_device_completion_items(kasa, tailwind, androidtv)
            pending = _completion_aliases_pending(self._discovery, "androidtv", "gotailwind", "kasa")
        elif ctx.command == "edit-mode":
            items = [CompletionAlias(display=name, matches=()) for name in _EDIT_MODE_SUBARGS]
            pending = False
        else:
            return

        yield from _yield_alias_completions(
            items=items,
            prefix=prefix,
            style=st,
            pending=pending,
        )

    def _live_managers(
        self,
    ) -> tuple[
        AndroidTvDeviceManager | None,
        KasaDeviceManager,
        SonosDeviceManager | None,
        GotailwindDeviceManager | None,
    ]:
        if self._discovery is not None:
            d = self._discovery
            return d.androidtv_mgr, d.kasa_mgr, d.sonos_mgr, d.tailwind_mgr
        return self._androidtv, self._kasa, self._sonos, self._tailwind


async def _repl_cmd_discover_androidtv(
    arg: str,
    *,
    androidtv_mgr: AndroidTvDeviceManager | None,
    androidtv_zeroconf_timeout: float,
    cache_path: Path | None,
    theme: _Theme,
) -> None:
    browse_timeout = float(androidtv_zeroconf_timeout)
    if androidtv_mgr is not None:
        browse_timeout = float(androidtv_mgr.zeroconf_timeout)
    tokens = [x.strip() for x in arg.split() if x.strip()]
    if len(tokens) == 1:
        try:
            browse_timeout = float(tokens[0])
        except ValueError:
            print(
                theme.err("Usage: discover-androidtv [browse_seconds]"),
                file=sys.stderr,
            )
            return
    elif tokens:
        print(
            theme.err("Usage: discover-androidtv [browse_seconds]"),
            file=sys.stderr,
        )
        return

    try:
        hits, mdns_labels, rows3 = await discover_cast_adb_specs_via_zeroconf(
            timeout=browse_timeout,
        )
    except Exception as ex:
        print(theme.err(f"Google Cast browse failed: {ex}"), file=sys.stderr)
        return
    if not hits:
        print(
            theme.dim(
                "  (no Cast devices found — same LAN/VLAN as this host? Try ANDROIDTV_HOSTS / --androidtv-host hints.)"
            )
        )
        return
    for uid in hits:
        lbl = mdns_labels.get(uid)
        if lbl:
            print(f"  {theme.ok(lbl)}  {theme.dim(uid)}")
        else:
            print(f"  {theme.ok(uid)}")
    if cache_path is not None:
        device_discovery_store.save_androidtv_hosts(cache_path, list(rows3))
        print(
            theme.dim(f"Saved {len(rows3)} endpoint(s) to discovery cache."),
            flush=True,
        )


async def _repl_cmd_discover_ep1(
    arg: str,
    *,
    ep1_mgr: Ep1DeviceManager | None,
    ep1_zeroconf_timeout: float,
    cache_path: Path | None,
    theme: _Theme,
) -> None:
    browse_timeout = float(ep1_zeroconf_timeout)
    if ep1_mgr is not None:
        browse_timeout = float(ep1_mgr.zeroconf_timeout)
    tokens = [x.strip() for x in arg.split() if x.strip()]
    if len(tokens) == 1:
        try:
            browse_timeout = float(tokens[0])
        except ValueError:
            print(
                theme.err("Usage: discover-ep1 [browse_seconds]"),
                file=sys.stderr,
            )
            return
    elif tokens:
        print(
            theme.err("Usage: discover-ep1 [browse_seconds]"),
            file=sys.stderr,
        )
        return

    try:
        hosts = await discover_ep1_hosts(timeout=browse_timeout)
    except Ep1DiscoveryError as ex:
        print(theme.err(str(ex)), file=sys.stderr)
        return
    except Exception as ex:
        print(theme.err(f"EP1 browse failed: {ex}"), file=sys.stderr)
        return

    for host, port in hosts:
        print(f"  {theme.ok(host)}{theme.dim(f':{port}')}")

    if ep1_mgr is not None:
        try:
            await ep1_mgr.rediscover(hosts=hosts)
            await _maybe_restart_device_state_watchers_after_ep1()
            for device in ep1_mgr.devices:
                print(
                    f"  {theme.device(device.preferred_label)} "
                    f"{theme.dim(device.identifier)} "
                    f"{theme.dim(_ep1_readings_summary(device))}"
                )
        except Exception as ex:
            print(theme.err(f"EP1 reconnect after browse failed: {ex}"), file=sys.stderr)
            return
        return

    # No live manager — one-shot connect for operator confirmation.
    mgr = Ep1DeviceManager(
        configured_hosts=hosts,
        discovery_cache_path=cache_path,
        zeroconf_discovery=False,
        noise_psk=None,
    )
    try:
        await mgr.fetch()
        for device in mgr.devices:
            print(
                f"  {theme.device(device.preferred_label)} "
                f"{theme.dim(device.identifier)} "
                f"{theme.dim(_ep1_readings_summary(device))}"
            )
        if not mgr.devices:
            print(theme.dim("  (hosts found but connect/read failed — check firmware / Noise PSK)"))
    finally:
        await mgr.disconnect()


async def _repl_cmd_read_ep1(
    arg: str,
    *,
    ep1_mgr: Ep1DeviceManager | None,
    theme: _Theme,
) -> None:
    """Live-reconnect and print occupancy / climate / lux for one or all EP1 sensors."""

    if ep1_mgr is None:
        print(
            theme.err(EP1_NOT_LOADED_MSG),
            file=sys.stderr,
        )
        return
    try:
        devices = list(ep1_mgr.devices)
    except NotInitializedError:
        print(theme.err(EP1_NOT_INITIALIZED_MSG), file=sys.stderr)
        return
    if not devices:
        print(theme.dim("  (no EP1 sensors connected)"))
        return

    needle = arg.strip()
    selected = devices
    if needle:
        triples = _collect_ep1_triples(ep1_mgr)
        api_id, amb, _meta = _resolve_cli_target(needle, triples)
        if api_id is None:
            _report_resolve_failure(theme, "EP1 sensor", needle, amb)
            return
        selected = [d for d in devices if d.identifier == api_id]
        if not selected:
            print(theme.err(f"EP1 sensor not found: {api_id}"), file=sys.stderr)
            return

    for device in selected:
        try:
            await ep1_mgr.refresh_device_readings(device.identifier)
        except Exception as ex:
            print(
                theme.err(f"{EP1_READ_FAILED_PREFIX}{device.preferred_label}: {ex}"),
                file=sys.stderr,
            )
            continue
        print(
            f"  {theme.device(device.preferred_label)} "
            f"{theme.dim(device.identifier)} "
            f"{theme.dim(f'{device.host}:{device.port}')} "
            f"{theme.state(_ep1_readings_summary(device))}"
        )


async def _repl_cmd_dispatch_switch(
    cmd: str,
    arg: str,
    *,
    kasa_mgr: KasaDeviceManager,
    androidtv_mgr: AndroidTvDeviceManager | None,
    theme: _Theme,
) -> None:
    triples_sw = _collect_switch_triples(kasa_mgr, androidtv_mgr)
    if not triples_sw:
        print(theme.err("No switch backends loaded."), file=sys.stderr)
        return
    api_id, amb, meta = _resolve_cli_target(arg.strip(), triples_sw)
    if api_id is None or meta is None:
        _report_resolve_failure(theme, "switch", arg.strip(), amb)
        return
    backend, _ = meta
    disp = api_id
    if backend == "kasa":
        hit = kasa_mgr.get_device_by_alias(api_id)
        if hit is not None:
            disp = hit.preferred_label
        if cmd == "is-on":
            on = await kasa_mgr.is_on(api_id)
            state = "on" if on else "off"
            st_fn = theme.ok if on else theme.meta
            print(f"{theme.device(repr(disp))} {theme.dim('->')} {st_fn(state)}")
            return
        if cmd == "turn-on":
            await kasa_mgr.turn_on(api_id)
        else:
            await kasa_mgr.turn_off(api_id)
    elif backend == "androidtv" and androidtv_mgr is not None:
        hit = androidtv_mgr.get_device_by_alias(api_id)
        if hit is not None:
            disp = hit.preferred_label
        if cmd == "is-on":
            on = await androidtv_mgr.is_on(api_id)
            state = "on" if on else "off"
            st_fn = theme.ok if on else theme.meta
            print(f"{theme.device(repr(disp))} {theme.dim('->')} {st_fn(state)}")
            return
        if cmd == "turn-on":
            await androidtv_mgr.turn_on(api_id)
        else:
            await androidtv_mgr.turn_off(api_id)
    else:
        print(theme.err("Switch backend not available."), file=sys.stderr)
        return

    if cmd == "turn-on":
        print(f"{theme.device(repr(disp))} {theme.dim('->')} {theme.ok('on')}")
    else:
        print(f"{theme.device(repr(disp))} {theme.dim('->')} {theme.meta('off')}")


def _lex_show_devices_key(label: str, tie: str) -> tuple[str, str]:
    """Case-folded primary label, then tie-breaker for stable lex order."""

    return (label.lower(), tie.lower())


def _print_device_identity(
    theme: _Theme,
    *,
    mac_address: str,
    host: str | None = None,
    details: list[str] | None = None,
) -> None:
    """Indented MAC / IP / family-specific identity under a ``show-devices`` row."""

    print(f"    {theme.meta('MAC address:')} {theme.device(mac_address)}")
    host_s = (host or "").strip()
    if host_s:
        print(f"    {theme.meta('IP:')} {theme.device(host_s)}")
    for line in details or []:
        text_line = line.strip()
        if text_line:
            print(f"    {theme.meta(text_line)}")


def _print_label_conflicts(theme: _Theme) -> None:
    conflicts = drain_device_label_conflicts()
    if not conflicts:
        return
    print(theme.header("Display name notices:"))
    for conflict in conflicts:
        print(f"  {theme.err(conflict.format_message())}")


async def _repl_cmd_show_devices(
    *,
    kasa_mgr: KasaDeviceManager,
    sonos_mgr: SonosDeviceManager | None,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None,
    ep1_mgr: Ep1DeviceManager | None,
    vizio_mgr: VizioDeviceManager | None,
    theme: _Theme,
    discovery: _CliDiscoverySession | None = None,
) -> None:
    print(theme.header("Google Cast (playback proxy: on = playing):"))
    if discovery is not None and discovery.families_pending("androidtv"):
        print(theme.dim(f"  ({COMPLETION_DISCOVERING_HINT})"))
    elif androidtv_mgr is None:
        print(
            theme.dim(
                "  (skipped — use --no-androidtv; otherwise ensure LAN Cast discovery or set ANDROIDTV_HOSTS / cache.)"
            )
        )
    else:
        try:
            devices = sorted(
                androidtv_mgr.switches,
                key=lambda d: _lex_show_devices_key(d.preferred_label, d.identifier),
            )
            if not devices:
                print(theme.dim("  (none connected — try discover-androidtv or explicit hosts.)"))
            for d in devices:
                print(f"  {theme.device(repr(d.preferred_label))}  {theme.state('(' + d.power_state + ')')}")
                cast_details = [f"uuid: {d.identifier}"] if d.identifier != d.mac_address else None
                _print_device_identity(
                    theme,
                    mac_address=d.mac_address,
                    host=getattr(d, "host", None),
                    details=cast_details,
                )
        except NotInitializedError:
            print(theme.dim("  (not available)"))
    print(theme.header("Kasa switches:"))
    if discovery is not None and discovery.families_pending("kasa"):
        print(theme.dim(f"  ({COMPLETION_DISCOVERING_HINT})"))
    else:
        try:
            rows = sorted(
                kasa_mgr.switches,
                key=lambda s: _lex_show_devices_key(s.preferred_label, s.identifier),
            )
            if not rows:
                print(theme.dim("  (none)"))
            for sw in rows:
                print(f"  {theme.device(repr(sw.preferred_label))}  {theme.state('(' + sw.power_state + ')')}")
                kasa_details: list[str] = []
                if sw.preferred_label != sw.mac_address:
                    kasa_details.append(f"alias: {sw.preferred_label}")
                _print_device_identity(
                    theme,
                    mac_address=sw.mac_address,
                    host=sw.host,
                    details=kasa_details,
                )
        except NotInitializedError:
            print(theme.dim("  (not available)"))
    print(theme.header("Sonos speakers:"))
    if discovery is not None and discovery.families_pending("sonos"):
        print(theme.dim(f"  ({COMPLETION_DISCOVERING_HINT})"))
    elif sonos_mgr is None:
        print(theme.dim("  (not loaded — omit --no-sonos or check LAN discovery.)"))
    else:
        try:
            players = sorted(
                sonos_mgr.players,
                key=lambda p: _lex_show_devices_key(p.preferred_label, p.identifier),
            )
            if not players:
                print(theme.dim("  (none discovered)"))
            else:
                playbacks = await asyncio.gather(*(asyncio.to_thread(p.transport_state_summary) for p in players))
                for p, playback in zip(players, playbacks):
                    print(f"  {theme.device(repr(p.preferred_label))}  {theme.state('(' + playback + ')')}")
                    sonos_details = [f"RINCON: {p.rincon_uid}"] if p.rincon_uid else None
                    _print_device_identity(
                        theme,
                        mac_address=p.mac_address,
                        host=p.host,
                        details=sonos_details,
                    )
        except NotInitializedError:
            print(theme.dim("  (not available)"))
    print(theme.header("Tailwind doors:"))
    if discovery is not None and discovery.families_pending("gotailwind"):
        print(theme.dim(f"  ({COMPLETION_DISCOVERING_HINT})"))
    elif tailwind_mgr is None:
        print(theme.dim("  (skipped — set TAILWIND_TOKEN or --tailwind-token)"))
    else:
        try:
            doors = sorted(
                tailwind_mgr.doors,
                key=lambda d: (
                    d.preferred_label.lower(),
                    int(d.door_index),
                    d.identifier.lower(),
                ),
            )
            if not doors:
                print(theme.dim("  (none)"))
            hub_host = (tailwind_mgr.host or "").strip() or None
            for d in doors:
                print(f"  {theme.device(repr(d.preferred_label))}  {theme.state('(' + d.door_state + ')')}")
                _print_device_identity(
                    theme,
                    mac_address=d.mac_address,
                    host=hub_host,
                    details=[f"door index: {d.door_index}", f"door id: {d.door_key}"],
                )
        except NotInitializedError:
            print(theme.dim("  (not available)"))
    print(theme.header("Everything Presence One:"))
    if discovery is not None and discovery.families_pending("ep1"):
        print(theme.dim(f"  ({COMPLETION_DISCOVERING_HINT})"))
    elif ep1_mgr is None:
        print(theme.dim("  (not loaded — set --ep1-host / EP1_HOSTS, or allow EP1 mDNS discovery.)"))
    else:
        try:
            sensors = sorted(
                ep1_mgr.devices,
                key=lambda d: _lex_show_devices_key(d.preferred_label, d.identifier),
            )
            if not sensors:
                print(theme.dim("  (none connected)"))
            for d in sensors:
                print(f"  {theme.device(repr(d.preferred_label))}  {theme.state('(' + d.occupancy_state + ')')}")
                _print_device_identity(
                    theme,
                    mac_address=d.mac_address if d.mac_address is not None else d.identifier,
                    host=d.host,
                    details=None,
                )
        except NotInitializedError:
            print(theme.dim("  (not available)"))
    print(theme.header("Vizio TVs:"))
    if discovery is not None and discovery.families_pending("vizio"):
        print(theme.dim(f"  ({COMPLETION_DISCOVERING_HINT})"))
    elif vizio_mgr is None:
        print(
            theme.dim("  (skipped — pair via settings, set VIZIO_HOSTS / --vizio-host, or configure VIZIO_AUTH_TOKEN.)")
        )
    else:
        try:
            tvs = sorted(
                vizio_mgr.tvs,
                key=lambda tv: _lex_show_devices_key(tv.preferred_label, tv.identifier),
            )
            if not tvs:
                print(theme.dim("  (none — pair while the TV is on, or turn on a cached TV via the UI/REPL.)"))
            for tv in tvs:
                print(f"  {theme.device(repr(tv.preferred_label))}  {theme.state('(' + tv.ui_power_state() + ')')}")
                host_meta = tv.endpoint.host
                if tv.endpoint.port != 7345:
                    host_meta = f"{host_meta}:{tv.endpoint.port}"
                vizio_details: list[str] = []
                model = (tv.endpoint.model or "").strip()
                if model:
                    vizio_details.append(f"model: {model}")
                _print_device_identity(
                    theme,
                    mac_address=tv.mac_address,
                    host=host_meta,
                    details=vizio_details,
                )
        except NotInitializedError:
            print(theme.dim("  (not available)"))
    _print_label_conflicts(theme)


async def _repl_cmd_sonos_pause_resume(
    cmd: str,
    arg: str,
    *,
    sonos_mgr: SonosDeviceManager | None,
    theme: _Theme,
) -> None:
    triples_pb = _collect_media_triples(sonos_mgr)
    if not triples_pb:
        print(
            theme.err("No Sonos speakers loaded (omit --no-sonos or check LAN discovery)."),
            file=sys.stderr,
        )
        return
    api_id, amb, meta = _resolve_cli_target(arg.strip(), triples_pb)
    if api_id is None or meta is None:
        _report_resolve_failure(theme, "Sonos speaker", arg.strip(), amb)
        return
    if sonos_mgr is None:
        print(theme.err("Sonos not configured."), file=sys.stderr)
        return
    hit = sonos_mgr.get_device_by_alias(api_id)
    disp = hit.preferred_label if hit is not None else api_id
    if cmd == "pause":
        await sonos_mgr.pause(api_id)
        print(f"{theme.device(repr(disp))} {theme.dim('->')} {theme.meta('paused')}")
    else:
        await sonos_mgr.resume(api_id)
        print(f"{theme.device(repr(disp))} {theme.dim('->')} {theme.ok('resumed')}")


async def dispatch_repl_action(
    kasa_mgr: KasaDeviceManager,
    sonos_mgr: SonosDeviceManager | None,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None,
    ep1_mgr: Ep1DeviceManager | None,
    vizio_mgr: VizioDeviceManager | None,
    *,
    cache_path: Path | None,
    androidtv_zeroconf_timeout: float,
    ep1_zeroconf_timeout: float,
    theme: _Theme,
    cmd: str,
    arg: str,
    discovery: _CliDiscoverySession | None = None,
) -> None:
    families = _DEVICE_COMMAND_FAMILIES.get(cmd)
    if families and discovery is not None and discovery.families_pending(*families):
        print(theme.err(DISCOVERY_IN_PROGRESS_MSG), file=sys.stderr)
        return
    if cmd == "set-display-name":
        if cache_path is None:
            print(
                theme.err("Persistence disabled; omit --no-discovery-cache to save display names."),
                file=sys.stderr,
            )
            return
        tokens = arg.split()
        triples = _collect_label_triples(kasa_mgr, tailwind_mgr, androidtv_mgr)
        got = _greedy_resolve_set_display_tokens(tokens, triples)
        if got is None:
            print(
                theme.err("Usage: set-display-name <device> <display name>"),
                file=sys.stderr,
            )
            return
        (backend, api_lookup_id), disp_name = got
        ck = _sqlite_canonical_key(backend, api_lookup_id, kasa_mgr, tailwind_mgr, androidtv_mgr)
        if ck is None:
            print(theme.err("Could not resolve device for persistence."), file=sys.stderr)
            return
        try:
            if backend == "kasa":
                kd = kasa_mgr.get_device_by_alias(api_lookup_id)
                if kd is None:
                    raise ValueError("missing kasa device")
                kd.set_display_name(disp_name)
                kasa_mgr.rebuild_lookup_after_display_change()
            elif backend == "tailwind" and tailwind_mgr is not None:
                gd = tailwind_mgr.get_device_by_alias(api_lookup_id)
                if gd is None:
                    raise ValueError("missing tailwind device")
                gd.set_display_name(disp_name)
                tailwind_mgr.rebuild_lookup_after_display_change()
            elif backend == "androidtv" and androidtv_mgr is not None:
                dev = androidtv_mgr.get_device_by_alias(api_lookup_id)
                if dev is None:
                    raise ValueError("missing Google Cast device")
                dev.set_display_name(disp_name)
                androidtv_mgr.rebuild_lookup_after_display_change()
        except ValueError:
            print(theme.err("Device not found after resolve."), file=sys.stderr)
            return
        device_discovery_store.upsert_display_name(
            cache_path,
            backend=backend,
            canonical_key=ck,
            display_name=disp_name,
        )
        print(
            f"{theme.dim('Display name for')} {theme.device(repr(ck))} "
            f"{theme.dim('(' + backend + ') ->')} {theme.ok(repr(disp_name))}"
        )
        return

    if cmd == "clear-display-name":
        if cache_path is None:
            print(
                theme.err("Persistence disabled; omit --no-discovery-cache."),
                file=sys.stderr,
            )
            return
        if not arg.strip():
            print(
                theme.err("Usage: clear-display-name <device>"),
                file=sys.stderr,
            )
            return
        triples = _collect_label_triples(kasa_mgr, tailwind_mgr, androidtv_mgr)
        api_lookup_id, amb, meta = _resolve_cli_target(arg.strip(), triples)
        if api_lookup_id is None or meta is None:
            _report_resolve_failure(theme, "device", arg.strip(), amb)
            return
        backend, _api = meta
        ck = _sqlite_canonical_key(backend, api_lookup_id, kasa_mgr, tailwind_mgr, androidtv_mgr)
        if ck is None:
            print(theme.err("Could not resolve device."), file=sys.stderr)
            return
        device_discovery_store.delete_display_name(cache_path, backend=backend, canonical_key=ck)
        try:
            if backend == "kasa":
                kd = kasa_mgr.get_device_by_alias(api_lookup_id)
                if kd:
                    kd.set_display_name(None)
                    kasa_mgr.rebuild_lookup_after_display_change()
            elif backend == "tailwind" and tailwind_mgr is not None:
                gd = tailwind_mgr.get_device_by_alias(api_lookup_id)
                if gd:
                    gd.set_display_name(None)
                    tailwind_mgr.rebuild_lookup_after_display_change()
            elif backend == "androidtv" and androidtv_mgr is not None:
                dev = androidtv_mgr.get_device_by_alias(api_lookup_id)
                if dev:
                    dev.set_display_name(None)
                    androidtv_mgr.rebuild_lookup_after_display_change()
        except (NotInitializedError, ValueError):
            pass
        print(f"{theme.dim('Cleared display name for')} {theme.device(repr(ck))} {theme.dim('(' + backend + ')')}")
        return

    if cmd == "show-devices":
        await _repl_cmd_show_devices(
            kasa_mgr=kasa_mgr,
            sonos_mgr=sonos_mgr,
            tailwind_mgr=tailwind_mgr,
            androidtv_mgr=androidtv_mgr,
            ep1_mgr=ep1_mgr,
            vizio_mgr=vizio_mgr,
            theme=theme,
            discovery=discovery,
        )
        return

    if cmd == "refresh":
        discos: list[Any] = []
        if androidtv_mgr is not None:
            discos.append(androidtv_mgr.disconnect())
        if ep1_mgr is not None:
            discos.append(ep1_mgr.disconnect())
        discos.append(kasa_mgr.disconnect())
        if sonos_mgr is not None:
            discos.append(sonos_mgr.disconnect())
        if tailwind_mgr is not None:
            discos.append(tailwind_mgr.disconnect())
        if vizio_mgr is not None:
            discos.append(vizio_mgr.disconnect())
        if discos:
            await asyncio.gather(*discos)

        async def ref_androidtv() -> dict[str, Any]:
            slug = "androidtv"
            if androidtv_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": FAMILY_SKIPPED_NOT_LOADED,
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await androidtv_mgr.fetch()
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": None,
                    "ok": True,
                    "mgr": None,
                }
            except Exception as ex:
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": ex,
                    "ok": False,
                    "mgr": None,
                }

        async def ref_ep1() -> dict[str, Any]:
            slug = "ep1"
            if ep1_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": FAMILY_SKIPPED_NOT_LOADED,
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await ep1_mgr.rediscover()
                await _maybe_restart_device_state_watchers_after_ep1()
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": None,
                    "ok": True,
                    "mgr": None,
                    "source": ep1_mgr.last_discovery_source,
                    "count": _ep1_sensor_count(ep1_mgr),
                }
            except Exception as ex:
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": ex,
                    "ok": False,
                    "mgr": None,
                }

        async def ref_kasa() -> dict[str, Any]:
            slug = "kasa"
            try:
                await kasa_mgr.fetch()
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": None,
                    "ok": True,
                    "mgr": None,
                }
            except Exception as ex:
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": ex,
                    "ok": False,
                    "mgr": None,
                }

        async def ref_sonos() -> dict[str, Any]:
            slug = "sonos"
            if sonos_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": FAMILY_SKIPPED_NOT_LOADED,
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await sonos_mgr.fetch()
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": None,
                    "ok": True,
                    "mgr": None,
                }
            except Exception as ex:
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": ex,
                    "ok": False,
                    "mgr": None,
                }

        async def ref_tailwind() -> dict[str, Any]:
            slug = "gotailwind"
            if tailwind_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": FAMILY_SKIPPED_NOT_LOADED,
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await tailwind_mgr.fetch()
                if cache_path is not None and tailwind_mgr.host:
                    device_discovery_store.save_tailwind_host(cache_path, tailwind_mgr.host)
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": None,
                    "ok": True,
                    "mgr": None,
                }
            except Exception as ex:
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": ex,
                    "ok": False,
                    "mgr": None,
                }

        async def ref_vizio() -> dict[str, Any]:
            slug = "vizio"
            if vizio_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": FAMILY_SKIPPED_NOT_LOADED,
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await vizio_mgr.fetch()
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": None,
                    "ok": True,
                    "mgr": None,
                    "source": vizio_mgr.last_discovery_source,
                    "count": _vizio_tv_count(vizio_mgr),
                }
            except Exception as ex:
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "",
                    "exc": ex,
                    "ok": False,
                    "mgr": None,
                }

        ref_bundles = await asyncio.gather(
            ref_androidtv(),
            ref_ep1(),
            ref_tailwind(),
            ref_kasa(),
            ref_sonos(),
            ref_vizio(),
        )
        ref_by = {b["slug"]: b for b in ref_bundles}
        for slug in _FAMILY_BOOT_SLUGS:
            _print_family_parallel_line(theme, slug, ref_by[slug], ok_verb="reconnected")
        nk = len(_kasa_switch_aliases(kasa_mgr))
        nz = _sonos_zone_count(sonos_mgr)
        na = _androidtv_switch_count(androidtv_mgr)
        ne = _ep1_sensor_count(ep1_mgr)
        nd = _tailwind_door_count(tailwind_mgr)
        nv = _vizio_tv_count(vizio_mgr)
        tail = (
            f"({na} Google Cast device(s), {ne} EP1 sensor(s), {nk} Kasa switch(es), {nz} Sonos speaker(s), "
            f"{nd} Tailwind door(s), {nv} Vizio TV(s))."
        )
        print(f"{theme.ok(REFRESH_DONE_PREFIX)} {theme.dim(tail)}")
        return

    if cmd == "kasa-creds":

        async def _toolkit_prompt(message: str, is_password: bool) -> str:
            # A fresh, completion-less PromptSession keeps the cred
            # input visually distinct from the regular REPL line — no
            # history, no completer, just a starred field.
            session = PromptSession()
            return await session.prompt_async(message, is_password=is_password)

        await _repl_cmd_kasa_creds(
            kasa_mgr,
            prompt_fn=_toolkit_prompt,
            theme=theme,
            cache_path=cache_path,
        )
        return

    if cmd == "setup-secrets":

        async def _secrets_prompt(message: str, is_password: bool) -> str:
            session = PromptSession()
            return await session.prompt_async(message, is_password=is_password)

        await _repl_cmd_setup_secrets(prompt_fn=_secrets_prompt, theme=theme)
        return

    if cmd == "refresh-discovery":
        from app.discovery_refresh import (
            NEW_DEVICE_FOUND_PREFIX,
            refresh_all_device_discovery,
        )

        state = DeviceManagersState(
            kasa_mgr=kasa_mgr,
            sonos_mgr=sonos_mgr,
            tailwind_mgr=tailwind_mgr,
            androidtv_mgr=androidtv_mgr,
            ep1_mgr=ep1_mgr,
            vizio_mgr=vizio_mgr,
            cache_path=cache_path,
            args=argparse.Namespace(),
        )
        result = await refresh_all_device_discovery(state, restart_watchers=True)
        for family in result.families:
            _print_family_parallel_line(
                theme,
                family.family_id,
                {
                    "slug": family.family_id,
                    "skipped": family.skipped,
                    "detail": family.skip_detail or "",
                    "exc": family.error,
                    "ok": family.ok,
                    "source": family.source,
                    "count": family.device_count if family.ok else None,
                },
                ok_verb="rediscovered",
            )
        for device in result.new_devices:
            print(f"{theme.ok(NEW_DEVICE_FOUND_PREFIX)} {device.display}")
        nk = len(_kasa_switch_aliases(kasa_mgr))
        nz = _sonos_zone_count(sonos_mgr)
        na = _androidtv_switch_count(androidtv_mgr)
        ne = _ep1_sensor_count(ep1_mgr)
        nd = _tailwind_door_count(tailwind_mgr)
        nv = _vizio_tv_count(vizio_mgr)
        tail = (
            f"({na} Google Cast device(s), {ne} EP1 sensor(s), {nk} Kasa switch(es), "
            f"{nz} Sonos speaker(s), {nd} Tailwind door(s), {nv} Vizio TV(s))."
        )
        print(f"{theme.ok('Discovery refreshed')} {theme.dim(tail)}")
        return

    if cmd == "discover-androidtv":
        await _repl_cmd_discover_androidtv(
            arg,
            androidtv_mgr=androidtv_mgr,
            androidtv_zeroconf_timeout=androidtv_zeroconf_timeout,
            cache_path=cache_path,
            theme=theme,
        )
        return

    if cmd == "discover-ep1":
        await _repl_cmd_discover_ep1(
            arg,
            ep1_mgr=ep1_mgr,
            ep1_zeroconf_timeout=ep1_zeroconf_timeout,
            cache_path=cache_path,
            theme=theme,
        )
        return

    if cmd == "read-ep1":
        await _repl_cmd_read_ep1(arg, ep1_mgr=ep1_mgr, theme=theme)
        return

    if not arg:
        print(theme.err(f"{cmd} requires a device name."), file=sys.stderr)
        return

    try:
        if cmd in ("turn-on", "turn-off", "is-on"):
            await _repl_cmd_dispatch_switch(
                cmd,
                arg,
                kasa_mgr=kasa_mgr,
                androidtv_mgr=androidtv_mgr,
                theme=theme,
            )
        elif cmd == "open-door":
            if tailwind_mgr is None:
                print(theme.err("Tailwind not configured."), file=sys.stderr)
                return
            api_id, amb, meta = _resolve_cli_target(
                arg,
                [t for t in _collect_label_triples(kasa_mgr, tailwind_mgr, androidtv_mgr) if t[1] == "tailwind"],
            )
            if api_id is None or meta is None:
                _report_resolve_failure(theme, "Tailwind door", arg, amb)
                return
            await tailwind_mgr.open(api_id)
            print(f"{theme.device(repr(api_id))} {theme.dim('->')} {theme.ok('open')} {theme.dim('(command sent)')}")
        elif cmd == "close-door":
            if tailwind_mgr is None:
                print(theme.err("Tailwind not configured."), file=sys.stderr)
                return
            api_id, amb, meta = _resolve_cli_target(
                arg,
                [t for t in _collect_label_triples(kasa_mgr, tailwind_mgr, androidtv_mgr) if t[1] == "tailwind"],
            )
            if api_id is None or meta is None:
                _report_resolve_failure(theme, "Tailwind door", arg, amb)
                return
            await tailwind_mgr.close(api_id)
            print(f"{theme.device(repr(api_id))} {theme.dim('->')} {theme.meta('close')} {theme.dim('(command sent)')}")
        elif cmd == "is-open":
            if tailwind_mgr is None:
                print(theme.err("Tailwind not configured."), file=sys.stderr)
                return
            api_id, amb, meta = _resolve_cli_target(
                arg,
                [t for t in _collect_label_triples(kasa_mgr, tailwind_mgr, androidtv_mgr) if t[1] == "tailwind"],
            )
            if api_id is None or meta is None:
                _report_resolve_failure(theme, "Tailwind door", arg, amb)
                return
            open_ = await tailwind_mgr.is_open(api_id)
            label = "open" if open_ else "closed"
            st_fn = theme.ok if open_ else theme.meta
            print(f"{theme.device(repr(api_id))} {theme.dim('->')} {st_fn(label)}")
        elif cmd == "pause":
            await _repl_cmd_sonos_pause_resume(
                cmd,
                arg,
                sonos_mgr=sonos_mgr,
                theme=theme,
            )
        elif cmd == "resume":
            await _repl_cmd_sonos_pause_resume(
                cmd,
                arg,
                sonos_mgr=sonos_mgr,
                theme=theme,
            )
    except NotInitializedError:
        print(
            theme.err("That backend is not initialized (discovery may have failed)."),
            file=sys.stderr,
        )
    except ValueError as ex:
        print(theme.err(str(ex)), file=sys.stderr)
    except Exception as ex:
        print(theme.err(f"Error: {ex}"), file=sys.stderr)


async def execute_line_for_api(
    kasa_mgr: KasaDeviceManager,
    sonos_mgr: SonosDeviceManager | None,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None,
    ep1_mgr: Ep1DeviceManager | None,
    vizio_mgr: VizioDeviceManager | None,
    *,
    cache_path: Path | None,
    androidtv_zeroconf_timeout: float,
    ep1_zeroconf_timeout: float,
    line: str,
) -> tuple[str, str, str | None]:
    """Execute one REPL line with plain output (for HTTP). Returns ``(stdout, stderr, error)``.

    ``error`` is set when the line is empty, unknown, or must be handled only in the local CLI
    (``exit`` / ``quit`` / ``edit-mode``).
    """

    s = line.strip()
    if not s:
        return "", "", "empty line"
    parsed = split_invocation(s)
    if parsed is None:
        return "", "", "unknown command"
    cmd, arg = parsed
    if cmd in ("exit", "quit"):
        return "", "", "not supported over HTTP"
    if cmd == "edit-mode":
        return "", "", "edit-mode is local to the CLI session"
    plain = _Theme(enabled=False)
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        if cmd == "help":
            _print_help(plain)
        else:
            await dispatch_repl_action(
                kasa_mgr,
                sonos_mgr,
                tailwind_mgr,
                androidtv_mgr,
                ep1_mgr,
                vizio_mgr,
                cache_path=cache_path,
                androidtv_zeroconf_timeout=androidtv_zeroconf_timeout,
                ep1_zeroconf_timeout=ep1_zeroconf_timeout,
                theme=plain,
                cmd=cmd,
                arg=arg,
            )
    return out_buf.getvalue(), err_buf.getvalue(), None


async def _cmd_loop(
    discovery: _CliDiscoverySession,
    *,
    editing_mode: EditingMode,
    theme: _Theme,
) -> None:
    prompt = PromptSession(
        completer=_ReplCompleter(
            androidtv=discovery.androidtv_mgr,
            kasa=discovery.kasa_mgr,
            sonos=discovery.sonos_mgr,
            tailwind=discovery.tailwind_mgr,
            theme=theme,
            discovery=discovery,
        ),
        complete_while_typing=False,
        editing_mode=editing_mode,
    )

    while True:
        try:
            line = await prompt.prompt_async(_repl_prompt_message(theme))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        parsed = split_invocation(line)
        if parsed is None:
            if line.strip():
                print(theme.err("Unknown command. Type `help`."), file=sys.stderr)
            continue

        cmd, arg = parsed

        if cmd in ("exit", "quit"):
            break

        if cmd == "help":
            _print_help(theme)
            continue

        if cmd == "edit-mode":
            sub = arg.strip().lower()
            if sub in ("emacs", "e"):
                prompt.editing_mode = EditingMode.EMACS
                print(theme.ok("Line editing: Emacs"))
            elif sub in ("vim", "vi", "v"):
                prompt.editing_mode = EditingMode.VI
                print(theme.ok("Line editing: Vim"))
            else:
                print(
                    theme.err("Usage: edit-mode emacs | vim"),
                    file=sys.stderr,
                )
            continue

        await dispatch_repl_action(
            discovery.kasa_mgr,
            discovery.sonos_mgr,
            discovery.tailwind_mgr,
            discovery.androidtv_mgr,
            discovery.ep1_mgr,
            discovery.vizio_mgr,
            cache_path=discovery.cache_path,
            androidtv_zeroconf_timeout=float(discovery.args.androidtv_zeroconf_timeout),
            ep1_zeroconf_timeout=float(getattr(discovery.args, "ep1_zeroconf_timeout", DEFAULT_EP1_ZEROCONF_TIMEOUT_S)),
            theme=theme,
            cmd=cmd,
            arg=arg,
            discovery=discovery,
        )


async def _cmd_loop_remote(
    base_url: str,
    api_key: str | None,
    *,
    editing_mode: EditingMode,
    theme: _Theme,
) -> None:
    headers: dict[str, str] = {}
    key = (api_key or "").strip()
    if key:
        headers["X-Domesti-Api-Key"] = key

    timeout = httpx.Timeout(120.0)
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
    ) as client:
        try:
            bundles = await _fetch_remote_completion_aliases(client)
        except httpx.HTTPStatusError as ex:
            print(
                theme.err(f"GET /v1/completion-aliases failed: HTTP {ex.response.status_code}"),
                file=sys.stderr,
            )
            detail = (ex.response.text or "").strip()
            if detail:
                print(theme.dim(detail[:800]), file=sys.stderr)
            raise SystemExit(1) from ex
        except httpx.RequestError as ex:
            print(theme.err(f"Cannot reach API at {base_url!r}: {ex}"), file=sys.stderr)
            raise SystemExit(1) from ex

        session = PromptSession(
            completer=_ReplCompleterRemote(bundles=bundles, theme=theme),
            complete_while_typing=False,
            editing_mode=editing_mode,
        )

        while True:
            try:
                line = await session.prompt_async(_repl_prompt_message(theme))
            except (EOFError, KeyboardInterrupt):
                print()
                break

            parsed = split_invocation(line)
            if parsed is None:
                if line.strip():
                    print(theme.err("Unknown command. Type `help`."), file=sys.stderr)
                continue

            cmd, arg = parsed

            if cmd in ("exit", "quit"):
                break

            if cmd == "help":
                _print_help(theme)
                continue

            if cmd == "edit-mode":
                sub = arg.strip().lower()
                if sub in ("emacs", "e"):
                    session.editing_mode = EditingMode.EMACS
                    print(theme.ok("Line editing: Emacs"))
                elif sub in ("vim", "vi", "v"):
                    session.editing_mode = EditingMode.VI
                    print(theme.ok("Line editing: Vim"))
                else:
                    print(
                        theme.err("Usage: edit-mode emacs | vim"),
                        file=sys.stderr,
                    )
                continue

            stripped = line.strip()
            try:
                resp = await client.post("/v1/execute-line", json={"line": stripped})
                resp.raise_for_status()
            except httpx.HTTPStatusError as ex:
                print(
                    theme.err(f"POST /v1/execute-line failed: HTTP {ex.response.status_code}"),
                    file=sys.stderr,
                )
                detail = (ex.response.text or "").strip()
                if detail:
                    print(theme.dim(detail[:800]), file=sys.stderr)
                continue
            except httpx.RequestError as ex:
                print(theme.err(f"Request failed: {ex}"), file=sys.stderr)
                continue

            payload = resp.json()
            out = payload.get("stdout") or ""
            err = payload.get("stderr") or ""
            api_err = payload.get("error")
            if api_err:
                print(theme.err(str(api_err)), file=sys.stderr)
            if out:
                sys.stdout.write(str(out))
            if err:
                sys.stderr.write(str(err))

            if cmd in ("refresh", "refresh-discovery") and not api_err:
                try:
                    bundles = await _fetch_remote_completion_aliases(client)
                    session.completer = _ReplCompleterRemote(bundles=bundles, theme=theme)
                except httpx.HTTPStatusError as ex:
                    print(
                        theme.err(f"GET /v1/completion-aliases after {cmd} failed: HTTP {ex.response.status_code}"),
                        file=sys.stderr,
                    )
                except httpx.RequestError as ex:
                    print(theme.err(f"completion-aliases refresh failed: {ex}"), file=sys.stderr)


def _parse_completion_alias_list(raw: object) -> list[CompletionAlias]:
    if not isinstance(raw, list):
        return []
    items: list[CompletionAlias] = []
    for entry in raw:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                items.append(CompletionAlias(display=text, matches=()))
            continue
        if not isinstance(entry, dict):
            continue
        display = str(entry.get("display") or "").strip()
        if not display:
            continue
        matches_raw = entry.get("matches") or []
        matches = tuple(str(m) for m in matches_raw if str(m).strip())
        items.append(CompletionAlias(display=display, matches=matches))
    return items


async def _fetch_remote_completion_aliases(client: httpx.AsyncClient) -> _RemoteAliasBundles:
    r = await client.get("/v1/completion-aliases")
    r.raise_for_status()
    data = r.json()
    return _RemoteAliasBundles(
        switch=_parse_completion_alias_list(data.get("switch")),
        sonos=_parse_completion_alias_list(data.get("sonos")),
        tailwind=_parse_completion_alias_list(data.get("tailwind")),
        all_device_labels=_parse_completion_alias_list(data.get("all_device_labels")),
    )


async def _bootstrap_tailwind(
    *,
    args: argparse.Namespace,
    cache_path: Path | None,
    theme: _Theme,
    token: str,
    log_failures: bool = True,
) -> tuple[GotailwindDeviceManager | None, BaseException | None]:
    """Try explicit/env host, then cached host, then mDNS; persist host after success.

    Returns ``(manager, None)`` on success, or ``(None, last_error)``. When ``log_failures`` is
    false, the caller is responsible for printing ``last_error`` (e.g. batched lex-order output).
    """
    explicit = (args.tailwind_host or "").strip()
    env_host = (os.environ.get("TAILWIND_HOST") or "").strip()
    candidates: list[str | None] = []
    seen: set[str | None] = set()

    def add(h: str | None) -> None:
        if h not in seen:
            seen.add(h)
            candidates.append(h)

    if explicit:
        add(explicit)
    elif env_host:
        add(env_host)
    elif cache_path is not None:
        cached = device_discovery_store.load_tailwind_host(cache_path)
        if cached:
            add(cached)
    add(None)

    last_exc: BaseException | None = None
    for host in candidates:
        mgr = GotailwindDeviceManager(
            token=token,
            host=host,
            discovery_timeout=float(args.tailwind_discovery_timeout),
            request_timeout=float(args.tailwind_request_timeout),
            display_names_store_path=cache_path,
        )
        try:
            await mgr.fetch()
            if cache_path is not None and mgr.host:
                device_discovery_store.save_tailwind_host(cache_path, mgr.host)
            return mgr, None
        except BaseException as ex:
            last_exc = ex
            await mgr.disconnect()

    if last_exc is not None and log_failures:
        print(theme.err(f"GoTailwind discovery failed: {last_exc}"), file=sys.stderr)
    return None, last_exc


class _CliDiscoverySession:
    """Mutable managers + per-family status while CLI discovery runs in the background."""

    def __init__(
        self,
        *,
        args: argparse.Namespace,
        cache_path: Path | None,
        kasa_mgr: KasaDeviceManager,
    ) -> None:
        self.args = args
        self.cache_path = cache_path
        self.kasa_mgr = kasa_mgr
        self.androidtv_mgr: AndroidTvDeviceManager | None = None
        self.ep1_mgr: Ep1DeviceManager | None = None
        self.sonos_mgr: SonosDeviceManager | None = None
        self.tailwind_mgr: GotailwindDeviceManager | None = None
        self.vizio_mgr: VizioDeviceManager | None = None
        self.family_status: dict[str, FamilyDiscoveryStatus] = {
            slug: FamilyDiscoveryStatus.PENDING for slug in _FAMILY_BOOT_SLUGS
        }

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> _CliDiscoverySession:
        clear_device_label_conflicts()
        cache_path = Path(args.discovery_cache).expanduser().resolve() if args.discovery_cache else None
        creds, _kasa_creds_source = resolve_kasa_credentials(cache_path=cache_path)
        kasa_mgr = KasaDeviceManager(
            discovery_target=args.discovery_target,
            discovery_timeout=args.discovery_timeout,
            credentials=creds,
            query_timeout=args.query_timeout,
            discovery_cache_path=cache_path,
            force_discovery=args.force_discovery,
        )
        return cls(args=args, cache_path=cache_path, kasa_mgr=kasa_mgr)

    def apply_family_result(self, slug: str, result: dict[str, Any]) -> None:
        if result.get("skipped"):
            status = FamilyDiscoveryStatus.SKIPPED
        elif result.get("exc") is not None:
            status = FamilyDiscoveryStatus.FAILED
        elif result.get("ok"):
            status = FamilyDiscoveryStatus.READY
        else:
            status = FamilyDiscoveryStatus.FAILED
        self.family_status[slug] = status
        attr = _SLUG_TO_MGR_ATTR.get(slug)
        if attr is not None:
            setattr(self, attr, result.get("mgr"))

    def fail_pending_families(self) -> None:
        for slug, status in self.family_status.items():
            if status is FamilyDiscoveryStatus.PENDING:
                self.family_status[slug] = FamilyDiscoveryStatus.FAILED

    def families_pending(self, *slugs: str) -> bool:
        return any(self.family_status[slug] is FamilyDiscoveryStatus.PENDING for slug in slugs)

    def to_state(self) -> DeviceManagersState:
        return DeviceManagersState(
            kasa_mgr=self.kasa_mgr,
            sonos_mgr=self.sonos_mgr,
            tailwind_mgr=self.tailwind_mgr,
            androidtv_mgr=self.androidtv_mgr,
            ep1_mgr=self.ep1_mgr,
            vizio_mgr=self.vizio_mgr,
            cache_path=self.cache_path,
            args=self.args,
        )


class DeviceManagersState(NamedTuple):
    """Live device managers plus paths after a successful :func:`bootstrap_device_managers`."""

    kasa_mgr: KasaDeviceManager
    sonos_mgr: SonosDeviceManager | None
    tailwind_mgr: GotailwindDeviceManager | None
    androidtv_mgr: AndroidTvDeviceManager | None
    ep1_mgr: Ep1DeviceManager | None
    vizio_mgr: VizioDeviceManager | None
    cache_path: Path | None
    args: argparse.Namespace


async def bootstrap_device_managers(
    args: argparse.Namespace,
    *,
    theme: _Theme,
    log_progress: bool = True,
    session: _CliDiscoverySession | None = None,
    exit_if_empty: bool = True,
    sync_kasa_vendor_aliases: bool = False,
) -> DeviceManagersState:
    """Create managers, run parallel discovery, and return state (or exit if nothing works).

    ``sync_kasa_vendor_aliases`` is CLI-only. HTTP discovery leaves Kasa
    hardware aliases unchanged.
    """

    if session is None:
        session = _CliDiscoverySession.from_args(args)
    cache_path = session.cache_path
    kasa_mgr = session.kasa_mgr
    args = session.args

    token, _tailwind_token_source = resolve_tailwind_token(
        cli_token=args.tailwind_token,
        cache_path=cache_path,
    )

    ep1_psk, _ep1_psk_source = resolve_ep1_noise_psk(
        cli_psk=getattr(args, "ep1_noise_psk", None),
        cache_path=cache_path,
    )
    ep1_hosts = _parse_ep1_host_specs(list(getattr(args, "ep1_host", None) or []))

    async def boot_androidtv() -> dict[str, Any]:
        slug = "androidtv"
        # TODO(google-cast-on-off): Cast turn_off is unreliable in the
        # field, so the bootstrap path is gated off at the source.
        # Flip ``ANDROIDTV_TEMPORARILY_DISABLED`` in
        # ``app.androidtv_device_manager`` (and remove this branch) once
        # the on/off behavior is verified end-to-end.
        if ANDROIDTV_TEMPORARILY_DISABLED:
            return {
                "slug": slug,
                "skipped": True,
                "detail": ANDROIDTV_TEMPORARILY_DISABLED_REASON,
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        if args.no_androidtv:
            return {
                "slug": slug,
                "skipped": True,
                "detail": "--no-androidtv",
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        host_specs = _merge_androidtv_host_specs(list(args.androidtv_host or []))
        cached_tv: list[tuple[str, int]] = []
        if cache_path is not None:
            cached_tv = device_discovery_store.load_androidtv_hosts(cache_path)
        want_zeroconf = AndroidTvDeviceManager.zeroconf_discovery_wanted(
            cli_opt_out=bool(args.no_androidtv_zeroconf),
        )
        if not (host_specs or want_zeroconf or cached_tv):
            return {
                "slug": slug,
                "skipped": True,
                "detail": (
                    "Cast browse disabled and no hosts or cache — use ANDROIDTV_HOSTS / "
                    "--androidtv-host, cache, or drop --no-androidtv-zeroconf"
                ),
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        mgr = AndroidTvDeviceManager(
            host_specs,
            connection_timeout=float(args.androidtv_connection_timeout),
            discovery_store_path=cache_path,
            zeroconf_discovery=want_zeroconf,
            zeroconf_timeout=float(args.androidtv_zeroconf_timeout),
        )
        try:
            await mgr.fetch()
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": None,
                "ok": True,
                "mgr": mgr,
                "source": mgr.last_discovery_source,
                "count": _androidtv_switch_count(mgr),
            }
        except Exception as ex:
            await mgr.disconnect()
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": ex,
                "ok": False,
                "mgr": None,
            }

    async def boot_kasa() -> dict[str, Any]:
        slug = "kasa"
        try:
            await kasa_mgr.fetch()
            if sync_kasa_vendor_aliases:
                try:
                    await kasa_mgr.sync_preferred_labels_to_vendor_aliases()
                except Exception:
                    _LOGGER.warning("Kasa preferred-label alias sync failed", exc_info=True)
            try:
                kasa_count = len(kasa_mgr.switches)
            except Exception:
                kasa_count = 0
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": None,
                "ok": True,
                "mgr": None,
                "source": kasa_mgr.last_discovery_source,
                "count": kasa_count,
            }
        except Exception as ex:
            await kasa_mgr.disconnect()
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": ex,
                "ok": False,
                "mgr": None,
            }

    async def boot_sonos() -> dict[str, Any]:
        slug = "sonos"
        if args.no_sonos:
            return {
                "slug": slug,
                "skipped": True,
                "detail": "--no-sonos",
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        mgr = SonosDeviceManager(
            discovery_timeout=float(args.sonos_discovery_timeout),
            discovery_cache_path=cache_path,
            force_discovery=bool(args.force_discovery),
        )
        try:
            await mgr.fetch()
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": None,
                "ok": True,
                "mgr": mgr,
                "source": mgr.last_discovery_source,
                "count": _sonos_zone_count(mgr),
            }
        except Exception as ex:
            await mgr.disconnect()
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": ex,
                "ok": False,
                "mgr": None,
            }

    vizio_hosts = configured_vizio_host_specs(
        cli_hosts=list(args.vizio_host or []),
        env_hosts=os.environ.get("VIZIO_HOSTS"),
    )
    vizio_env_token = (os.environ.get("VIZIO_AUTH_TOKEN") or "").strip() or None

    async def boot_vizio() -> dict[str, Any]:
        slug = "vizio"
        if args.no_vizio:
            return {
                "slug": slug,
                "skipped": True,
                "detail": "--no-vizio",
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        if not _vizio_targets_available(cache_path, vizio_hosts):
            return {
                "slug": slug,
                "skipped": True,
                "detail": "no hosts — set VIZIO_HOSTS or --vizio-host",
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        if not _vizio_has_any_auth(
            cache_path,
            cli_token=args.vizio_auth_token,
            env_token=vizio_env_token,
        ):
            return {
                "slug": slug,
                "skipped": True,
                "detail": "no auth token — pair via settings or set VIZIO_AUTH_TOKEN",
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        mgr = VizioDeviceManager(
            configured_hosts=vizio_hosts,
            discovery_cache_path=cache_path,
            cli_auth_token=args.vizio_auth_token,
            env_auth_token=vizio_env_token,
            force_discovery=bool(args.force_discovery),
        )
        try:
            await mgr.fetch()
            count = _vizio_tv_count(mgr)
            if count == 0:
                await mgr.disconnect()
                return {
                    "slug": slug,
                    "skipped": False,
                    "detail": "no TVs connected — check hosts and auth tokens",
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": None,
                "ok": True,
                "mgr": mgr,
                "source": mgr.last_discovery_source,
                "count": count,
            }
        except Exception as ex:
            await mgr.disconnect()
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": ex,
                "ok": False,
                "mgr": None,
            }

    async def boot_ep1() -> dict[str, Any]:
        slug = "ep1"
        cached_ep1: list[tuple[str, int, str | None, str | None]] = []
        if cache_path is not None:
            cached_ep1 = device_discovery_store.load_ep1_devices(cache_path)
        want_zeroconf = not bool(getattr(args, "no_ep1_zeroconf", False))
        if not ep1_hosts and not cached_ep1 and not want_zeroconf:
            return {
                "slug": slug,
                "skipped": True,
                "detail": "no hosts — set --ep1-host / EP1_HOSTS or allow EP1 mDNS",
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        mgr = Ep1DeviceManager(
            configured_hosts=ep1_hosts,
            discovery_cache_path=cache_path,
            cli_noise_psk=getattr(args, "ep1_noise_psk", None),
            noise_psk=ep1_psk or None,
            force_discovery=bool(args.force_discovery),
            zeroconf_discovery=want_zeroconf,
            zeroconf_timeout=float(getattr(args, "ep1_zeroconf_timeout", DEFAULT_EP1_ZEROCONF_TIMEOUT_S)),
        )
        try:
            await mgr.fetch()
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": None,
                "ok": True,
                "mgr": mgr,
                "source": mgr.last_discovery_source,
                "count": len(mgr.devices),
            }
        except Exception as ex:
            await mgr.disconnect()
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": ex,
                "ok": False,
                "mgr": None,
            }

    async def boot_tailwind() -> dict[str, Any]:
        slug = "gotailwind"
        if not token:
            return {
                "slug": slug,
                "skipped": True,
                "detail": "no token — set TAILWIND_TOKEN or --tailwind-token",
                "exc": None,
                "ok": False,
                "mgr": None,
            }
        mgr, last_exc = await _bootstrap_tailwind(
            args=args,
            cache_path=cache_path,
            theme=theme,
            token=token,
            log_failures=False,
        )
        if mgr is not None:
            return {
                "slug": slug,
                "skipped": False,
                "detail": "",
                "exc": None,
                "ok": True,
                "mgr": mgr,
                # Tailwind uses an HTTP API (no LAN broadcast), so "cache" vs
                # "discovery" doesn't apply. Leave ``source`` unset and the
                # renderer will simply omit it.
                "source": None,
                "count": _tailwind_door_count(mgr),
            }
        return {
            "slug": slug,
            "skipped": False,
            "detail": "",
            "exc": last_exc or RuntimeError("GoTailwind discovery failed"),
            "ok": False,
            "mgr": None,
        }

    if log_progress:
        print(theme.warn("Discovering devices (parallel)…"), flush=True)
        _LOGGER.info("[startup] discovering devices (parallel)")
    bundles = await asyncio.gather(
        _timed_family_boot(
            "androidtv",
            boot_androidtv(),
            log_progress=log_progress,
            theme=theme,
            session=session,
        ),
        _timed_family_boot(
            "ep1",
            boot_ep1(),
            log_progress=log_progress,
            theme=theme,
            session=session,
        ),
        _timed_family_boot(
            "gotailwind",
            boot_tailwind(),
            log_progress=log_progress,
            theme=theme,
            session=session,
        ),
        _timed_family_boot(
            "kasa",
            boot_kasa(),
            log_progress=log_progress,
            theme=theme,
            session=session,
        ),
        _timed_family_boot(
            "sonos",
            boot_sonos(),
            log_progress=log_progress,
            theme=theme,
            session=session,
        ),
        _timed_family_boot(
            "vizio",
            boot_vizio(),
            log_progress=log_progress,
            theme=theme,
            session=session,
        ),
    )
    by_slug = {b["slug"]: b for b in bundles}

    androidtv_mgr = session.androidtv_mgr
    ep1_mgr = session.ep1_mgr
    sonos_mgr = session.sonos_mgr
    tailwind_mgr = session.tailwind_mgr
    vizio_mgr = session.vizio_mgr
    kasa_ok = bool(by_slug["kasa"].get("ok"))
    tw_ok = tailwind_mgr is not None

    sonos_ready = sonos_mgr is not None
    androidtv_ready = androidtv_mgr is not None
    ep1_ready = ep1_mgr is not None
    vizio_ready = vizio_mgr is not None
    if not kasa_ok and not tw_ok and not sonos_ready and not androidtv_ready and not ep1_ready and not vizio_ready:
        if exit_if_empty:
            print(theme.err(NO_BACKENDS_EXITING_MSG), file=sys.stderr)
            raise SystemExit(1)
        print(theme.err(NO_BACKENDS_DEVICE_COMMANDS_UNAVAILABLE_MSG), file=sys.stderr)

    if log_progress:
        ns = _kasa_switch_count(kasa_mgr)
        nz = _sonos_zone_count(sonos_mgr)
        na = _androidtv_switch_count(androidtv_mgr)
        ne = _ep1_sensor_count(ep1_mgr)
        nd = _tailwind_door_count(tailwind_mgr)
        nv = _vizio_tv_count(vizio_mgr)
        tail = (
            f"({na} Google Cast device(s), {ne} EP1 sensor(s), {ns} Kasa switch(es), {nz} Sonos speaker(s), "
            f"{nd} Tailwind door(s), {nv} Vizio TV(s)). Tab-complete commands and names."
        )
        print(f"{theme.ok('Ready')} {theme.dim(tail)}", flush=True)
        _print_label_conflicts(theme)

    return session.to_state()


_SHUTDOWN_DISCONNECT_TIMEOUT_S = 15.0


async def _disconnect_backend_on_shutdown(
    backend: str,
    disconnect: Awaitable[None],
) -> None:
    try:
        await asyncio.wait_for(disconnect, timeout=_SHUTDOWN_DISCONNECT_TIMEOUT_S)
    except asyncio.TimeoutError:
        _LOGGER.warning(
            "[shutdown] %s disconnect timed out after %.1fs",
            backend,
            _SHUTDOWN_DISCONNECT_TIMEOUT_S,
        )
    except Exception:
        _LOGGER.warning("[shutdown] %s disconnect failed", backend, exc_info=True)


async def shutdown_device_managers(state: DeviceManagersState) -> None:
    disconnect_tasks: list[asyncio.Task[None]] = []
    if state.androidtv_mgr is not None:
        disconnect_tasks.append(
            asyncio.create_task(
                _disconnect_backend_on_shutdown(
                    "androidtv",
                    state.androidtv_mgr.disconnect(),
                )
            )
        )
    if state.ep1_mgr is not None:
        disconnect_tasks.append(asyncio.create_task(_disconnect_backend_on_shutdown("ep1", state.ep1_mgr.disconnect())))
    disconnect_tasks.append(asyncio.create_task(_disconnect_backend_on_shutdown("kasa", state.kasa_mgr.disconnect())))
    if state.sonos_mgr is not None:
        disconnect_tasks.append(
            asyncio.create_task(_disconnect_backend_on_shutdown("sonos", state.sonos_mgr.disconnect()))
        )
    if state.tailwind_mgr is not None:
        disconnect_tasks.append(
            asyncio.create_task(
                _disconnect_backend_on_shutdown(
                    "tailwind",
                    state.tailwind_mgr.disconnect(),
                )
            )
        )
    if state.vizio_mgr is not None:
        disconnect_tasks.append(
            asyncio.create_task(_disconnect_backend_on_shutdown("vizio", state.vizio_mgr.disconnect()))
        )
    if disconnect_tasks:
        await asyncio.gather(*disconnect_tasks)


async def _async_main(args: argparse.Namespace) -> None:
    theme = _Theme(enabled=_stdout_color_enabled(args.color))
    discovery = _CliDiscoverySession.from_args(args)

    async def _run_discovery() -> None:
        try:
            await bootstrap_device_managers(
                args,
                theme=theme,
                log_progress=True,
                session=discovery,
                exit_if_empty=False,
                sync_kasa_vendor_aliases=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            discovery.fail_pending_families()
            _LOGGER.exception("Device discovery failed")
            print(theme.err(f"{DISCOVERY_FAILED_PREFIX}{ex}"), file=sys.stderr, flush=True)

    discovery_task = asyncio.create_task(
        _run_discovery(),
        name="cli-device-discovery",
    )
    try:
        with patch_stdout(raw=True):
            await _cmd_loop(
                discovery,
                editing_mode=_editing_mode_enum(args.edit_mode),
                theme=theme,
            )
    finally:
        if not discovery_task.done():
            discovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await discovery_task
        await shutdown_device_managers(discovery.to_state())


async def _async_main_remote(args: argparse.Namespace) -> None:
    theme = _Theme(enabled=_stdout_color_enabled(args.color))
    base = (args.api_base_url or "").strip().rstrip("/")
    if not base:
        print(
            "Remote mode requires --api-base-url or DEVICE_MANAGER_API_URL.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(theme.warn(f"Remote REPL (HTTP) — {base}"), flush=True)
    await _cmd_loop_remote(
        base,
        args.api_key,
        editing_mode=_editing_mode_enum(args.edit_mode),
        theme=theme,
    )


def build_arg_parser(*, add_help: bool = True, add_version: bool = True) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=("Interactive REPL for Google Cast, Kasa switches, Sonos speakers, and GoTailwind garage doors."),
        add_help=add_help,
    )
    p.add_argument(
        "--androidtv-connection-timeout",
        type=float,
        default=20.0,
        metavar="SEC",
        help="PyChromecast socket wait() / command timeout per device (default: 20)",
    )
    p.add_argument(
        "--androidtv-host",
        action="append",
        default=None,
        metavar="HOST[:PORT]",
        help=(
            "Known Cast host or IP for faster discovery (port optional, ignored as a hint; "
            "repeatable). Also ANDROIDTV_HOSTS (comma-separated)."
        ),
    )
    p.add_argument(
        "--no-androidtv-zeroconf",
        action="store_true",
        help=(
            "Do not run an open-ended Cast mDNS browse — only explicit and cached hosts. "
            "Default is full LAN browse; also ANDROIDTV_ZEROCONF=0|false|no|off."
        ),
    )
    p.add_argument(
        "--androidtv-zeroconf-timeout",
        type=float,
        default=12.0,
        metavar="SEC",
        help="Cast mDNS discovery window per browse (default: 12)",
    )
    p.add_argument(
        "--api-base-url",
        type=str,
        default=(os.environ.get("DEVICE_MANAGER_API_URL") or "").strip() or None,
        metavar="URL",
        help=(
            "Use a remote domesti HTTP API instead of local hardware "
            "(e.g. http://192.168.1.10:8003). Also DEVICE_MANAGER_API_URL."
        ),
    )
    p.add_argument(
        "--api-key",
        type=str,
        default=(os.environ.get("DEVICE_MANAGER_API_KEY") or "").strip() or None,
        metavar="TOKEN",
        help="Optional X-Domesti-Api-Key when using --api-base-url. Also DEVICE_MANAGER_API_KEY.",
    )
    p.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help=("Terminal colors in the REPL (default: auto when stdout is a TTY). Disabled when NO_COLOR is set."),
    )
    p.add_argument(
        "--discovery-cache",
        type=str,
        default=str(DEFAULT_DISCOVERY_DB),
        metavar="PATH",
        help=(
            "SQLite DB for Kasa configs and last Tailwind host "
            f"(default: {DEFAULT_DISCOVERY_DB}). "
            "Ignored when --no-discovery-cache is passed."
        ),
    )
    p.add_argument(
        "--discovery-target",
        type=str,
        default=None,
        metavar="ADDR",
        help="Kasa broadcast target (e.g. 192.168.1.255)",
    )
    p.add_argument(
        "--discovery-timeout",
        type=int,
        default=5,
        metavar="SEC",
        help="Kasa UDP discovery timeout (default: 5)",
    )
    p.add_argument(
        "--edit-mode",
        choices=("emacs", "vim"),
        default=_normalize_edit_mode_choice(os.environ.get("DEVICE_MANAGER_EDIT_MODE")),
        help=(
            "REPL line-editing bindings (default: vim; DEVICE_MANAGER_EDIT_MODE can "
            "set emacs / e / vim / vi / v). CLI overrides the env default."
        ),
    )
    p.add_argument(
        "--force-discovery",
        action="store_true",
        help="Always run UDP Kasa discovery (ignore cache for initial fetch)",
    )
    p.add_argument(
        "--no-discovery-cache",
        action="store_true",
        help="Do not read or write the SQLite discovery database",
    )
    p.add_argument(
        "--no-sonos",
        action="store_true",
        help="Do not discover or control Sonos speakers",
    )
    p.add_argument(
        "--no-androidtv",
        action="store_true",
        help=(
            "Do not discover or control Google Cast targets. "
            "Note: Google Cast bring-up is currently disabled regardless "
            "of this flag — see ANDROIDTV_TEMPORARILY_DISABLED in "
            "app.androidtv_device_manager (TODO: google-cast-on-off)."
        ),
    )
    p.add_argument(
        "--query-timeout",
        type=int,
        default=None,
        metavar="SEC",
        help="Kasa per-query timeout override",
    )
    p.add_argument(
        "--sonos-discovery-timeout",
        type=float,
        default=5.0,
        metavar="SEC",
        help="Sonos UDP discovery window per attempt (default: 5)",
    )
    p.add_argument(
        "--tailwind-token",
        type=str,
        default=None,
        metavar="KEY",
        help="Tailwind Local Control Key (default: TAILWIND_TOKEN env)",
    )
    p.add_argument(
        "--tailwind-host",
        type=str,
        default=None,
        metavar="HOST",
        help="Tailwind controller host/IP (default: TAILWIND_HOST or mDNS discovery)",
    )
    p.add_argument(
        "--tailwind-discovery-timeout",
        type=float,
        default=12.0,
        metavar="SEC",
        help="Tailwind mDNS discovery timeout when host unset (default: 12)",
    )
    p.add_argument(
        "--tailwind-request-timeout",
        type=float,
        default=8.0,
        metavar="SEC",
        help="Tailwind HTTP request timeout (default: 8)",
    )
    p.add_argument(
        "--ep1-host",
        action="append",
        default=None,
        metavar="HOST[:PORT]",
        help=(
            "Known Everything Presence One host or IP (port optional, default 6053; repeatable). "
            "Also EP1_HOSTS (comma-separated). When unset, mDNS discovers EP1 on the LAN."
        ),
    )
    p.add_argument(
        "--ep1-noise-psk",
        type=str,
        default=(os.environ.get("EP1_NOISE_PSK") or "").strip() or None,
        metavar="PSK",
        help=(
            "ESPHome API Noise pre-shared key (optional for Homey / plaintext firmware). "
            "Also EP1_NOISE_PSK env or Settings → EP1."
        ),
    )
    p.add_argument(
        "--ep1-zeroconf-timeout",
        type=float,
        default=DEFAULT_EP1_ZEROCONF_TIMEOUT_S,
        metavar="SEC",
        help=f"EP1 ESPHome mDNS browse window (default: {DEFAULT_EP1_ZEROCONF_TIMEOUT_S:g})",
    )
    p.add_argument(
        "--no-ep1-zeroconf",
        action="store_true",
        help="Do not browse for EP1 via mDNS — only --ep1-host / EP1_HOSTS and discovery cache.",
    )
    p.add_argument(
        "--no-vizio",
        action="store_true",
        help="Do not discover or control Vizio SmartCast TVs",
    )
    p.add_argument(
        "--vizio-auth-token",
        type=str,
        default=(os.environ.get("VIZIO_AUTH_TOKEN") or "").strip() or None,
        metavar="TOKEN",
        help="SmartCast auth token for all configured TVs (default: VIZIO_AUTH_TOKEN env)",
    )
    p.add_argument(
        "--vizio-host",
        action="append",
        default=None,
        metavar="HOST[:PORT]",
        help=(
            "Known Vizio TV host or IP (port optional, default 7345; repeatable). Also VIZIO_HOSTS (comma-separated)."
        ),
    )
    if add_version:
        p.add_argument(
            "--version",
            action="version",
            version=format_cli_version_line(prog="domesti-bot"),
        )
    return p


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.no_discovery_cache:
        args.discovery_cache = None
    api_url = (args.api_base_url or "").strip()
    if api_url:
        asyncio.run(_async_main_remote(args))
    else:
        asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
