"""Interactive REPL for Google Cast, TP-Link Kasa switches, Sonos zones, and GoTailwind doors.

Run::

    uv run python -m app.domesti_bot_cli

Or from the repo root (uses ``uv`` when available so the project venv stays in sync)::

    ./scripts/domesti-bot

Credentials:

* Optional ``KASA_USERNAME`` / ``KASA_PASSWORD`` (both) for Kasa/Tapo KLAP.
* Tailwind **Local Control Key**: ``TAILWIND_TOKEN`` or ``--tailwind-token`` (see
  :mod:`app.gotailwind_device_manager`).

* **Sonos**: zones on your LAN (S1-class UPnP stacks included) via optional ``soco``;
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
import httpx
import io
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, NamedTuple

from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.completion import Completion, Completer
from prompt_toolkit.enums import EditingMode

from app import kasa_discovery_store
from app.androidtv_device_manager import (
    ANDROIDTV_TEMPORARILY_DISABLED,
    ANDROIDTV_TEMPORARILY_DISABLED_REASON,
    AndroidTvDeviceManager,
    discover_cast_adb_specs_via_zeroconf,
    _merge_androidtv_host_specs,
)
from app.device_manager import NotInitializedError
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.sonos_device_manager import SonosDeviceManager

COMMANDS = (
    "clear-display-name",
    "close-door",
    "discover-androidtv",
    "edit-mode",
    "exit",
    "help",
    "is-on",
    "is-open",
    "kasa-creds",
    "open-door",
    "pause",
    "quit",
    "refresh",
    "refresh-discovery",
    "resume",
    "set-display-name",
    "show-devices",
    "turn-off",
    "turn-on",
)

DEFAULT_DISCOVERY_DB = Path.home() / ".cache" / "rule-engine" / "device_discovery.sqlite"

_COMMAND_HELP_LINES: tuple[tuple[str, str], ...] = (
    ("clear-display-name", "Drop the saved friendly label for a device (SQLite cache required)."),
    ("close-door", "Tell Tailwind to fully close a door (match name, index, or id)."),
    (
        "discover-androidtv",
        "Cast mDNS browse (PyChromecast); optional seconds timeout; cache when SQLite on.",
    ),
    ("edit-mode", "Switch Emacs vs Vim keys for this session: edit-mode emacs | vim."),
    ("exit", "Leave the REPL."),
    ("help", "Show this list."),
    ("is-on", "Print whether a Kasa switch or Cast target is on (media playing) or off."),
    ("is-open", "Print whether a Tailwind door reads fully open."),
    (
        "kasa-creds",
        "Prompt for Kasa/Tapo account email + password (password hidden) and rediscover.",
    ),
    ("open-door", "Tell Tailwind to fully open a door."),
    ("pause", "Pause playback on a Sonos zone."),
    ("quit", "Leave the REPL (same as exit)."),
    ("refresh", "Reconnect all backends; Kasa may reuse cached discovery."),
    ("refresh-discovery", "Full LAN discovery: Google Cast, Kasa, Sonos, Tailwind."),
    ("resume", "Resume playback on a Sonos zone."),
    ("set-display-name", "Save a friendly label for a device (SQLite cache required)."),
    ("show-devices", "List Google Cast targets, Kasa switches, Sonos zones, then Tailwind doors."),
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
_FAMILY_BOOT_SLUGS: tuple[str, ...] = ("androidtv", "gotailwind", "kasa", "sonos")
_FAMILY_BOOT_LABEL: dict[str, str] = {
    "androidtv": "Google Cast",
    "gotailwind": "GoTailwind",
    "kasa": "Kasa",
    "sonos": "Sonos",
}
# Plural unit name used in the per-backend "ready" line. Singular forms are not
# needed because the count is shown as a bare integer (``"0 zones"``, ``"1 zones"``).
_FAMILY_UNIT_PLURAL: dict[str, str] = {
    "androidtv": "devices",
    "gotailwind": "doors",
    "kasa": "switches",
    "sonos": "zones",
}
# Human-friendly label for the ``last_discovery_source`` signal each backend
# attaches to its boot bundle. ``None`` (e.g. Tailwind, which has no LAN
# discovery) prints no source annotation at all.
_FAMILY_SOURCE_LABEL: dict[str, str] = {
    "cache": "cache",
    "discovery": "LAN discovery",
}


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
    try:
        labels: set[str] = set()
        for s in mgr.switches:
            labels.add(s.identifier)
            labels.add(s.preferred_label)
        return sorted(labels)
    except NotInitializedError:
        return []


def _maybe_print_kasa_auth_notice(
    kasa_mgr: KasaDeviceManager, *, theme: _Theme
) -> None:
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
    print(
        f"  {theme.dim('or set KASA_USERNAME + KASA_PASSWORD env vars before restart.')}"
    )


async def _repl_cmd_kasa_creds(
    kasa_mgr: KasaDeviceManager,
    *,
    prompt_fn: Callable[[str, bool], Awaitable[str]],
    theme: _Theme,
) -> None:
    """Interactive Kasa credential entry + rediscover (driven by ``prompt_fn``).

    ``prompt_fn(message, is_password)`` is the injection point: the
    REPL wires it to a fresh :class:`prompt_toolkit.PromptSession`'s
    ``prompt_async`` so the password field is starred. Tests pass a
    canned-answer function so this helper stays exercisable without
    prompt_toolkit's terminal layer.

    Credentials are stored only in memory on the manager — to persist
    across restarts the user still needs to set ``KASA_USERNAME`` and
    ``KASA_PASSWORD`` in their environment (or the systemd
    ``EnvironmentFile=``).
    """

    print(
        f"{theme.header('Kasa credentials')} "
        f"{theme.dim('(password hidden — to persist, set KASA_USERNAME / KASA_PASSWORD env vars before restart)')}"
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
    print(theme.dim("kasa-creds: rediscovering Kasa devices…"))
    try:
        await kasa_mgr.rediscover()
    except Exception as ex:
        print(theme.err(f"kasa-creds: rediscover failed: {ex}"), file=sys.stderr)
        return
    n_switches = len(_kasa_switch_aliases(kasa_mgr))
    skipped = kasa_mgr.skipped_auth_hosts
    if skipped:
        print(
            theme.warn(
                f"kasa-creds: {len(skipped)} device(s) still failed auth: "
                f"{', '.join(skipped)}"
            )
        )
        print(f"  {theme.dim('Likely a wrong account email/password.')}")
    print(theme.ok(f"Kasa: ready ({n_switches} switch(es))"))


def _all_cli_device_labels(
    kasa_mgr: KasaDeviceManager,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None = None,
) -> list[str]:
    labels: set[str] = set()
    labels.update(_androidtv_switch_aliases(androidtv_mgr))
    labels.update(_kasa_switch_aliases(kasa_mgr))
    if tailwind_mgr is not None:
        labels.update(_tailwind_door_aliases(tailwind_mgr))
    return sorted(labels)


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
                triples.append((d.identifier, "androidtv", d.identifier))
                if d.preferred_label != d.identifier:
                    triples.append((d.preferred_label, "androidtv", d.identifier))
        except NotInitializedError:
            pass
    try:
        for kd in kasa_mgr.switches:
            triples.append((kd.identifier, "kasa", kd.identifier))
            if kd.preferred_label != kd.identifier:
                triples.append((kd.preferred_label, "kasa", kd.identifier))
    except NotInitializedError:
        pass
    if tailwind_mgr is not None:
        try:
            for gd in tailwind_mgr.doors:
                triples.append((gd.identifier, "tailwind", gd.identifier))
                triples.append((str(gd.door_index), "tailwind", gd.identifier))
                if gd.preferred_label not in (gd.identifier, str(gd.door_index)):
                    triples.append((gd.preferred_label, "tailwind", gd.identifier))
        except NotInitializedError:
            pass
    return triples


def _editing_mode_enum(mode: str) -> EditingMode:
    return EditingMode.VI if mode == "vim" else EditingMode.EMACS


def _resolve_cli_target(
    raw: str,
    triples: list[tuple[str, str, str]],
) -> tuple[str | None, list[str], tuple[str, str] | None]:
    """Return ``(api_lookup_id, ambiguous_labels, (backend, api_id))``."""

    labels = [t[0] for t in triples]
    hit, amb = _resolve_device_name(raw, labels)
    if hit is None:
        return None, amb, None
    for lab, backend, api in triples:
        if lab.lower() == hit.lower():
            return api, [], (backend, api)
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
        return kd._kDevice.host if kd is not None else None
    if backend == "tailwind" and tailwind_mgr is not None:
        gd = tailwind_mgr.get_device_by_alias(api_lookup_id)
        return gd.identifier if gd is not None else None
    if backend == "androidtv" and androidtv_mgr is not None:
        dev = androidtv_mgr.get_device_by_alias(api_lookup_id)
        return dev.identifier if dev is not None else None
    return None


def _sonos_zone_aliases(mgr: SonosDeviceManager | None) -> list[str]:
    if mgr is None:
        return []
    try:
        labels: set[str] = set()
        for p in mgr.players:
            labels.add(p.identifier)
            labels.add(p.preferred_label)
        return sorted(labels)
    except NotInitializedError:
        return []


def _sonos_zone_count(mgr: SonosDeviceManager | None) -> int:
    if mgr is None:
        return 0
    try:
        return len(mgr.players)
    except NotInitializedError:
        return 0


def _androidtv_switch_aliases(mgr: AndroidTvDeviceManager | None) -> list[str]:
    if mgr is None:
        return []
    try:
        labels: set[str] = set()
        for d in mgr.switches:
            labels.add(d.identifier)
            labels.add(d.preferred_label)
        return sorted(labels)
    except NotInitializedError:
        return []


def _androidtv_switch_count(mgr: AndroidTvDeviceManager | None) -> int:
    if mgr is None:
        return 0
    try:
        return len(mgr.switches)
    except NotInitializedError:
        return 0


def _collect_media_triples(sonos_mgr: SonosDeviceManager | None) -> list[tuple[str, str, str]]:
    triples: list[tuple[str, str, str]] = []
    if sonos_mgr is not None:
        try:
            for p in sonos_mgr.players:
                triples.append((p.identifier, "sonos", p.identifier))
                if p.preferred_label != p.identifier:
                    triples.append((p.preferred_label, "sonos", p.identifier))
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
                triples.append((d.identifier, "androidtv", d.identifier))
                if d.preferred_label != d.identifier:
                    triples.append((d.preferred_label, "androidtv", d.identifier))
        except NotInitializedError:
            pass
    try:
        for kd in kasa_mgr.switches:
            triples.append((kd.identifier, "kasa", kd.identifier))
            if kd.preferred_label != kd.identifier:
                triples.append((kd.preferred_label, "kasa", kd.identifier))
    except NotInitializedError:
        pass
    return triples


def _media_playback_aliases(sonos_mgr: SonosDeviceManager | None) -> list[str]:
    return list(_sonos_zone_aliases(sonos_mgr))


def _switch_aliases(
    kasa_mgr: KasaDeviceManager,
    androidtv_mgr: AndroidTvDeviceManager | None,
) -> list[str]:
    labels: set[str] = set()
    labels.update(_androidtv_switch_aliases(androidtv_mgr))
    labels.update(_kasa_switch_aliases(kasa_mgr))
    return sorted(labels)


def _resolve_device_name(
    raw: str, candidates: list[str]
) -> tuple[str | None, list[str]]:
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
    return HTML(
        '<style fg="ansicyan"><b>device_manager</b></style>'
        '<style fg="ansibrightblack"> &gt; </style>'
    )


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
    if mgr is None:
        return []
    try:
        ids: set[str] = set()
        for d in mgr.doors:
            ids.add(d.identifier)
            ids.add(str(d.door_index))
            ids.add(d.preferred_label)
        return sorted(ids)
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
    switch: list[str]
    sonos: list[str]
    tailwind: list[str]
    all_device_labels: list[str]


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
            "exit",
            "quit",
            "help",
            "show-devices",
            "refresh",
            "refresh-discovery",
        ):
            return

        aliases: list[str]
        if ctx.command in ("turn-on", "turn-off", "is-on"):
            aliases = self._bundles.switch
        elif ctx.command in ("pause", "resume"):
            aliases = self._bundles.sonos
        elif ctx.command in ("open-door", "close-door", "is-open"):
            aliases = self._bundles.tailwind
        elif ctx.command in ("set-display-name", "clear-display-name"):
            aliases = self._bundles.all_device_labels
        elif ctx.command == "edit-mode":
            aliases = list(_EDIT_MODE_SUBARGS)
        else:
            return

        prefix = ctx.arg_prefix
        prefix_lower = prefix.lower()
        st = self._theme.completion_parameter_style()
        for name in aliases:
            if name.lower().startswith(prefix_lower):
                yield Completion(name, start_position=-len(prefix), style=st)


class _ReplCompleter(Completer):
    def __init__(
        self,
        *,
        androidtv: AndroidTvDeviceManager | None,
        kasa: KasaDeviceManager,
        sonos: SonosDeviceManager | None,
        tailwind: GotailwindDeviceManager | None,
        theme: _Theme,
    ) -> None:
        self._androidtv = androidtv
        self._kasa = kasa
        self._sonos = sonos
        self._tailwind = tailwind
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
            "exit",
            "quit",
            "help",
            "show-devices",
            "refresh",
            "refresh-discovery",
        ):
            return

        aliases: list[str]
        if ctx.command in ("turn-on", "turn-off", "is-on"):
            aliases = _switch_aliases(self._kasa, self._androidtv)
        elif ctx.command in ("pause", "resume"):
            aliases = _media_playback_aliases(self._sonos)
        elif ctx.command in ("open-door", "close-door", "is-open"):
            aliases = _tailwind_door_aliases(self._tailwind)
        elif ctx.command in ("set-display-name", "clear-display-name"):
            aliases = _all_cli_device_labels(self._kasa, self._tailwind, self._androidtv)
        elif ctx.command == "edit-mode":
            aliases = list(_EDIT_MODE_SUBARGS)
        else:
            return

        prefix = ctx.arg_prefix
        prefix_lower = prefix.lower()
        st = self._theme.completion_parameter_style()
        for name in aliases:
            if name.lower().startswith(prefix_lower):
                yield Completion(name, start_position=-len(prefix), style=st)


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
                "  (no Cast devices found — same LAN/VLAN as this host? "
                "Try ANDROIDTV_HOSTS / --androidtv-host hints.)"
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
        kasa_discovery_store.save_androidtv_hosts(cache_path, list(rows3))
        print(
            theme.dim(f"Saved {len(rows3)} endpoint(s) to discovery cache."),
            flush=True,
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


async def _repl_cmd_show_devices(
    *,
    kasa_mgr: KasaDeviceManager,
    sonos_mgr: SonosDeviceManager | None,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None,
    theme: _Theme,
) -> None:
    print(theme.header("Google Cast (playback proxy: on = playing):"))
    if androidtv_mgr is None:
        print(
            theme.dim(
                "  (skipped — use --no-androidtv; otherwise ensure LAN Cast discovery "
                "or set ANDROIDTV_HOSTS / cache.)"
            )
        )
    else:
        try:
            devices = sorted(
                androidtv_mgr.switches,
                key=lambda d: _lex_show_devices_key(d.preferred_label, d.identifier),
            )
            if not devices:
                print(
                    theme.dim(
                        "  (none connected — try discover-androidtv or explicit hosts.)"
                    )
                )
            for d in devices:
                if d.preferred_label != d.identifier:
                    print(
                        f"  {theme.device(repr(d.preferred_label))}  "
                        f"{theme.meta('[uuid')} {theme.device(repr(d.identifier))}"
                        f"{theme.meta(']')} {theme.state('(' + d.power_state + ')')}"
                    )
                else:
                    print(
                        f"  {theme.device(repr(d.identifier))}  "
                        f"{theme.state('(' + d.power_state + ')')}"
                    )
        except NotInitializedError:
            print(theme.dim("  (not available)"))
    print(theme.header("Kasa switches:"))
    try:
        rows = sorted(
            kasa_mgr.switches,
            key=lambda s: _lex_show_devices_key(s.preferred_label, s.identifier),
        )
        if not rows:
            print(theme.dim("  (none)"))
        for sw in rows:
            if sw.display_name:
                print(
                    f"  {theme.device(repr(sw.preferred_label))}  "
                    f"{theme.meta('[alias')} {theme.device(repr(sw.identifier))}"
                    f"{theme.meta(']')} {theme.state('(' + sw.power_state + ')')}"
                )
            else:
                print(
                    f"  {theme.device(repr(sw.identifier))}  "
                    f"{theme.state('(' + sw.power_state + ')')}"
                )
    except NotInitializedError:
        print(theme.dim("  (not available)"))
    print(theme.header("Sonos zones:"))
    if sonos_mgr is None:
        print(
            theme.dim(
                "  (not loaded — omit --no-sonos or check LAN discovery.)"
            )
        )
    else:
        try:
            players = sorted(
                sonos_mgr.players,
                key=lambda p: _lex_show_devices_key(p.preferred_label, p.identifier),
            )
            if not players:
                print(theme.dim("  (none discovered)"))
            else:
                playbacks = await asyncio.gather(
                    *(
                        asyncio.to_thread(p.transport_state_summary)
                        for p in players
                    )
                )
                for p, playback in zip(players, playbacks):
                    print(
                        f"  {theme.device(repr(p.preferred_label))}  "
                        f"{theme.meta('[uid')} {theme.meta(p.identifier)}{theme.meta(']')} "
                        f"{theme.state('(' + playback + ')')}"
                    )
        except NotInitializedError:
            print(theme.dim("  (not available)"))
    print(theme.header("Tailwind doors:"))
    if tailwind_mgr is None:
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
            for d in doors:
                if d.display_name:
                    print(
                        f"  {theme.device(repr(d.preferred_label))}  "
                        f"{theme.meta('[id')} {theme.device(repr(d.identifier))}"
                        f"{theme.meta(', index')} {theme.meta(str(d.door_index))}"
                        f"{theme.meta(']')} {theme.state('(' + d.door_state + ')')}"
                    )
                else:
                    print(
                        f"  {theme.device(repr(d.identifier))}  "
                        f"{theme.meta('[index')} {theme.meta(str(d.door_index))}"
                        f"{theme.meta(']')} {theme.state('(' + d.door_state + ')')}"
                    )
        except NotInitializedError:
            print(theme.dim("  (not available)"))


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
            theme.err(
                "No Sonos zones loaded (omit --no-sonos or check LAN discovery)."
            ),
            file=sys.stderr,
        )
        return
    api_id, amb, meta = _resolve_cli_target(arg.strip(), triples_pb)
    if api_id is None or meta is None:
        _report_resolve_failure(theme, "Sonos zone", arg.strip(), amb)
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
    *,
    cache_path: Path | None,
    androidtv_zeroconf_timeout: float,
    theme: _Theme,
    cmd: str,
    arg: str,
) -> None:
    if cmd == "set-display-name":
        if cache_path is None:
            print(
                theme.err(
                    "Persistence disabled; omit --no-discovery-cache to save display names."
                ),
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
        ck = _sqlite_canonical_key(
            backend, api_lookup_id, kasa_mgr, tailwind_mgr, androidtv_mgr
        )
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
        kasa_discovery_store.upsert_display_name(
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
                theme.err(
                    "Persistence disabled; omit --no-discovery-cache."
                ),
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
        ck = _sqlite_canonical_key(
            backend, api_lookup_id, kasa_mgr, tailwind_mgr, androidtv_mgr
        )
        if ck is None:
            print(theme.err("Could not resolve device."), file=sys.stderr)
            return
        kasa_discovery_store.delete_display_name(
            cache_path, backend=backend, canonical_key=ck
        )
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
        print(
            f"{theme.dim('Cleared display name for')} "
            f"{theme.device(repr(ck))} {theme.dim('(' + backend + ')')}"
        )
        return

    if cmd == "show-devices":
        await _repl_cmd_show_devices(
            kasa_mgr=kasa_mgr,
            sonos_mgr=sonos_mgr,
            tailwind_mgr=tailwind_mgr,
            androidtv_mgr=androidtv_mgr,
            theme=theme,
        )
        return

    if cmd == "refresh":
        discos: list[Any] = []
        if androidtv_mgr is not None:
            discos.append(androidtv_mgr.disconnect())
        discos.append(kasa_mgr.disconnect())
        if sonos_mgr is not None:
            discos.append(sonos_mgr.disconnect())
        if tailwind_mgr is not None:
            discos.append(tailwind_mgr.disconnect())
        if discos:
            await asyncio.gather(*discos)

        async def ref_androidtv() -> dict[str, Any]:
            slug = "androidtv"
            if androidtv_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": "not loaded",
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
                    "detail": "not loaded",
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
                    "detail": "not loaded",
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await tailwind_mgr.fetch()
                if cache_path is not None and tailwind_mgr.host:
                    kasa_discovery_store.save_tailwind_host(
                        cache_path, tailwind_mgr.host
                    )
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

        ref_bundles = await asyncio.gather(
            ref_androidtv(),
            ref_tailwind(),
            ref_kasa(),
            ref_sonos(),
        )
        ref_by = {b["slug"]: b for b in ref_bundles}
        for slug in _FAMILY_BOOT_SLUGS:
            _print_family_parallel_line(
                theme, slug, ref_by[slug], ok_verb="reconnected"
            )
        nk = len(_kasa_switch_aliases(kasa_mgr))
        nz = _sonos_zone_count(sonos_mgr)
        na = _androidtv_switch_count(androidtv_mgr)
        nd = _tailwind_door_count(tailwind_mgr)
        tail = (
            f"({na} Google Cast device(s), {nk} Kasa switch(es), {nz} Sonos zone(s), "
            f"{nd} Tailwind door(s))."
        )
        print(f"{theme.ok('Refreshed')} {theme.dim(tail)}")
        return

    if cmd == "kasa-creds":
        async def _toolkit_prompt(message: str, is_password: bool) -> str:
            # A fresh, completion-less PromptSession keeps the cred
            # input visually distinct from the regular REPL line — no
            # history, no completer, just a starred field.
            session = PromptSession()
            return await session.prompt_async(message, is_password=is_password)

        await _repl_cmd_kasa_creds(
            kasa_mgr, prompt_fn=_toolkit_prompt, theme=theme
        )
        return

    if cmd == "refresh-discovery":
        async def rd_androidtv() -> dict[str, Any]:
            slug = "androidtv"
            if androidtv_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": "not loaded",
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await androidtv_mgr.rediscover()
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

        async def rd_kasa() -> dict[str, Any]:
            slug = "kasa"
            try:
                await kasa_mgr.rediscover()
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

        async def rd_sonos() -> dict[str, Any]:
            slug = "sonos"
            if sonos_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": "not loaded",
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await sonos_mgr.rediscover()
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

        async def rd_tailwind() -> dict[str, Any]:
            slug = "gotailwind"
            if tailwind_mgr is None:
                return {
                    "slug": slug,
                    "skipped": True,
                    "detail": "not loaded",
                    "exc": None,
                    "ok": False,
                    "mgr": None,
                }
            try:
                await tailwind_mgr.rediscover()
                if cache_path is not None and tailwind_mgr.host:
                    kasa_discovery_store.save_tailwind_host(
                        cache_path, tailwind_mgr.host
                    )
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

        rd_bundles = await asyncio.gather(
            rd_androidtv(),
            rd_tailwind(),
            rd_kasa(),
            rd_sonos(),
        )
        rd_by = {b["slug"]: b for b in rd_bundles}
        for slug in _FAMILY_BOOT_SLUGS:
            _print_family_parallel_line(
                theme, slug, rd_by[slug], ok_verb="rediscovered"
            )
        nk = len(_kasa_switch_aliases(kasa_mgr))
        nz = _sonos_zone_count(sonos_mgr)
        na = _androidtv_switch_count(androidtv_mgr)
        nd = _tailwind_door_count(tailwind_mgr)
        tail = (
            f"({na} Google Cast device(s), {nk} Kasa switch(es), {nz} Sonos zone(s), "
            f"{nd} Tailwind door(s))."
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
            key, amb = _resolve_device_name(arg, _tailwind_door_aliases(tailwind_mgr))
            if key is None:
                _report_resolve_failure(theme, "Tailwind door", arg, amb)
                return
            await tailwind_mgr.open(key)
            print(
                f"{theme.device(repr(key))} {theme.dim('->')} "
                f"{theme.ok('open')} {theme.dim('(command sent)')}"
            )
        elif cmd == "close-door":
            if tailwind_mgr is None:
                print(theme.err("Tailwind not configured."), file=sys.stderr)
                return
            key, amb = _resolve_device_name(arg, _tailwind_door_aliases(tailwind_mgr))
            if key is None:
                _report_resolve_failure(theme, "Tailwind door", arg, amb)
                return
            await tailwind_mgr.close(key)
            print(
                f"{theme.device(repr(key))} {theme.dim('->')} "
                f"{theme.meta('close')} {theme.dim('(command sent)')}"
            )
        elif cmd == "is-open":
            if tailwind_mgr is None:
                print(theme.err("Tailwind not configured."), file=sys.stderr)
                return
            key, amb = _resolve_device_name(arg, _tailwind_door_aliases(tailwind_mgr))
            if key is None:
                _report_resolve_failure(theme, "Tailwind door", arg, amb)
                return
            open_ = await tailwind_mgr.is_open(key)
            label = "open" if open_ else "closed"
            st_fn = theme.ok if open_ else theme.meta
            print(
                f"{theme.device(repr(key))} {theme.dim('->')} {st_fn(label)}"
            )
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
            theme.err(
                "That backend is not initialized (discovery may have failed)."
            ),
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
    *,
    cache_path: Path | None,
    androidtv_zeroconf_timeout: float,
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
                cache_path=cache_path,
                androidtv_zeroconf_timeout=androidtv_zeroconf_timeout,
                theme=plain,
                cmd=cmd,
                arg=arg,
            )
    return out_buf.getvalue(), err_buf.getvalue(), None


async def _cmd_loop(
    kasa_mgr: KasaDeviceManager,
    sonos_mgr: SonosDeviceManager | None,
    tailwind_mgr: GotailwindDeviceManager | None,
    androidtv_mgr: AndroidTvDeviceManager | None,
    *,
    cache_path: Path | None,
    androidtv_zeroconf_timeout: float,
    editing_mode: EditingMode,
    theme: _Theme,
) -> None:
    session = PromptSession(
        completer=_ReplCompleter(
            androidtv=androidtv_mgr,
            kasa=kasa_mgr,
            sonos=sonos_mgr,
            tailwind=tailwind_mgr,
            theme=theme,
        ),
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

        await dispatch_repl_action(
            kasa_mgr,
            sonos_mgr,
            tailwind_mgr,
            androidtv_mgr,
            cache_path=cache_path,
            androidtv_zeroconf_timeout=androidtv_zeroconf_timeout,
            theme=theme,
            cmd=cmd,
            arg=arg,
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
                        theme.err(
                            f"GET /v1/completion-aliases after {cmd} failed: "
                            f"HTTP {ex.response.status_code}"
                        ),
                        file=sys.stderr,
                    )
                except httpx.RequestError as ex:
                    print(theme.err(f"completion-aliases refresh failed: {ex}"), file=sys.stderr)


async def _fetch_remote_completion_aliases(client: httpx.AsyncClient) -> _RemoteAliasBundles:
    r = await client.get("/v1/completion-aliases")
    r.raise_for_status()
    data = r.json()
    return _RemoteAliasBundles(
        switch=list(data.get("switch") or []),
        sonos=list(data.get("sonos") or []),
        tailwind=list(data.get("tailwind") or []),
        all_device_labels=list(data.get("all_device_labels") or []),
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
        cached = kasa_discovery_store.load_tailwind_host(cache_path)
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
                kasa_discovery_store.save_tailwind_host(cache_path, mgr.host)
            return mgr, None
        except BaseException as ex:
            last_exc = ex
            await mgr.disconnect()

    if last_exc is not None and log_failures:
        print(theme.err(f"GoTailwind discovery failed: {last_exc}"), file=sys.stderr)
    return None, last_exc


class DeviceManagersState(NamedTuple):
    """Live device managers plus paths after a successful :func:`bootstrap_device_managers`."""

    kasa_mgr: KasaDeviceManager
    sonos_mgr: SonosDeviceManager | None
    tailwind_mgr: GotailwindDeviceManager | None
    androidtv_mgr: AndroidTvDeviceManager | None
    cache_path: Path | None
    args: argparse.Namespace


async def bootstrap_device_managers(
    args: argparse.Namespace,
    *,
    theme: _Theme,
    log_progress: bool = True,
) -> DeviceManagersState:
    """Create managers, run parallel discovery, and return state (or exit if nothing works)."""

    cache_path = Path(args.discovery_cache).expanduser().resolve() if args.discovery_cache else None
    creds = KasaDeviceManager.credentials_from_env()

    kasa_mgr = KasaDeviceManager(
        discovery_target=args.discovery_target,
        discovery_timeout=args.discovery_timeout,
        credentials=creds,
        query_timeout=args.query_timeout,
        discovery_cache_path=cache_path,
        force_discovery=args.force_discovery,
    )

    token = (args.tailwind_token or os.environ.get("TAILWIND_TOKEN") or "").strip()

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
            cached_tv = kasa_discovery_store.load_androidtv_hosts(cache_path)
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
    bundles = await asyncio.gather(
        boot_androidtv(),
        boot_tailwind(),
        boot_kasa(),
        boot_sonos(),
    )
    by_slug = {b["slug"]: b for b in bundles}
    if log_progress:
        for slug in _FAMILY_BOOT_SLUGS:
            _print_family_parallel_line(theme, slug, by_slug[slug], ok_verb="ready")

    androidtv_mgr = by_slug["androidtv"].get("mgr")
    sonos_mgr = by_slug["sonos"].get("mgr")
    tailwind_mgr = by_slug["gotailwind"].get("mgr")
    kasa_ok = bool(by_slug["kasa"].get("ok"))
    tw_ok = tailwind_mgr is not None

    sonos_ready = sonos_mgr is not None
    androidtv_ready = androidtv_mgr is not None
    if not kasa_ok and not tw_ok and not sonos_ready and not androidtv_ready:
        print(theme.err("No backends initialized; exiting."), file=sys.stderr)
        raise SystemExit(1)

    if log_progress:
        ns = len(_kasa_switch_aliases(kasa_mgr))
        nz = _sonos_zone_count(sonos_mgr)
        na = _androidtv_switch_count(androidtv_mgr)
        nd = _tailwind_door_count(tailwind_mgr)
        tail = (
            f"({na} Google Cast device(s), {ns} Kasa switch(es), {nz} Sonos zone(s), "
            f"{nd} Tailwind door(s)). Tab-complete commands and names."
        )
        print(f"{theme.ok('Ready')} {theme.dim(tail)}", flush=True)
        _maybe_print_kasa_auth_notice(kasa_mgr, theme=theme)

    return DeviceManagersState(
        kasa_mgr=kasa_mgr,
        sonos_mgr=sonos_mgr,
        tailwind_mgr=tailwind_mgr,
        androidtv_mgr=androidtv_mgr,
        cache_path=cache_path,
        args=args,
    )


async def shutdown_device_managers(state: DeviceManagersState) -> None:
    if state.androidtv_mgr is not None:
        await state.androidtv_mgr.disconnect()
    await state.kasa_mgr.disconnect()
    if state.sonos_mgr is not None:
        await state.sonos_mgr.disconnect()
    if state.tailwind_mgr is not None:
        await state.tailwind_mgr.disconnect()


async def _async_main(args: argparse.Namespace) -> None:
    theme = _Theme(enabled=_stdout_color_enabled(args.color))
    state = await bootstrap_device_managers(args, theme=theme, log_progress=True)
    try:
        await _cmd_loop(
            state.kasa_mgr,
            state.sonos_mgr,
            state.tailwind_mgr,
            state.androidtv_mgr,
            cache_path=state.cache_path,
            androidtv_zeroconf_timeout=float(state.args.androidtv_zeroconf_timeout),
            editing_mode=_editing_mode_enum(args.edit_mode),
            theme=theme,
        )
    finally:
        await shutdown_device_managers(state)


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


def build_arg_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Interactive REPL for Google Cast, Kasa switches, Sonos zones, "
            "and GoTailwind garage doors."
        ),
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
            "(e.g. http://192.168.1.10:8765). Also DEVICE_MANAGER_API_URL."
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
        help=(
            "Terminal colors in the REPL (default: auto when stdout is a TTY). "
            "Disabled when NO_COLOR is set."
        ),
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
        help="Do not discover or control Sonos zones",
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
