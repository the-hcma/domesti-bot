"""Interactive REPL for TP-Link Kasa switches, GoTailwind garage doors, and Sonos zones.

Run::

    python device_manager_cli.py

Or from the repo root (after ``chmod +x``); uses ``uv`` when available so the project venv stays in sync::

    ./device-manager

Credentials:

* Optional ``KASA_USERNAME`` / ``KASA_PASSWORD`` (both) for Kasa/Tapo KLAP.
* Tailwind **Local Control Key**: ``TAILWIND_TOKEN`` or ``--tailwind-token`` (see
  :mod:`gotailwind_device_manager`).

* **Sonos**: zones on your LAN (S1-class UPnP stacks included) via optional ``soco``;
  use ``pause`` / ``resume`` in the REPL. Pass ``--no-sonos`` to skip discovery.

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
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import NamedTuple

from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.completion import Completion, Completer
from prompt_toolkit.enums import EditingMode

from device_manager import NotInitializedError
from gotailwind_device_manager import GotailwindDeviceManager
import kasa_discovery_store
from kasa_device_manager import KasaDeviceManager
from sonos_device_manager import SonosDeviceManager

COMMANDS = (
    "clear-display-name",
    "close-door",
    "edit-mode",
    "exit",
    "help",
    "is-on",
    "is-open",
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
    ("edit-mode", "Switch Emacs vs Vim keys for this session: edit-mode emacs | vim."),
    ("exit", "Leave the REPL."),
    ("help", "Show this list."),
    ("is-on", "Print whether a Kasa switch is on or off."),
    ("is-open", "Print whether a Tailwind door reads fully open."),
    ("open-door", "Tell Tailwind to fully open a door."),
    ("pause", "Pause whatever is playing on a Sonos zone."),
    ("quit", "Leave the REPL (same as exit)."),
    ("refresh", "Reconnect all backends; Kasa may reuse cached discovery."),
    ("refresh-discovery", "Full LAN discovery for Kasa; reload Tailwind and Sonos zones."),
    ("resume", "Resume playback on a Sonos zone (continues the queue)."),
    ("set-display-name", "Save a friendly label for a device (SQLite cache required)."),
    ("show-devices", "List Kasa switches, Sonos zones, and Tailwind doors with status."),
    ("turn-off", "Turn a Kasa switch off."),
    ("turn-on", "Turn a Kasa switch on."),
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
            "Sonos: pause / resume by zone name or uid (omit --no-sonos). "
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


def _all_cli_device_labels(
    kasa_mgr: KasaDeviceManager,
    tailwind_mgr: GotailwindDeviceManager | None,
) -> list[str]:
    labels: set[str] = set()
    labels.update(_kasa_switch_aliases(kasa_mgr))
    if tailwind_mgr is not None:
        labels.update(_tailwind_door_aliases(tailwind_mgr))
    return sorted(labels)


def _collect_label_triples(
    kasa_mgr: KasaDeviceManager,
    tailwind_mgr: GotailwindDeviceManager | None,
) -> list[tuple[str, str, str]]:
    """``(surface_label, backend, api_lookup_id)`` — api id is Kasa hardware alias or Tailwind ``door_id``."""

    triples: list[tuple[str, str, str]] = []
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
) -> str | None:
    if backend == "kasa":
        kd = kasa_mgr.get_device_by_alias(api_lookup_id)
        return kd._kDevice.host if kd is not None else None
    if backend == "tailwind" and tailwind_mgr is not None:
        gd = tailwind_mgr.get_device_by_alias(api_lookup_id)
        return gd.identifier if gd is not None else None
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


class _ReplCompleter(Completer):
    def __init__(
        self,
        *,
        kasa: KasaDeviceManager,
        sonos: SonosDeviceManager | None,
        tailwind: GotailwindDeviceManager | None,
        theme: _Theme,
    ) -> None:
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
            aliases = _kasa_switch_aliases(self._kasa)
        elif ctx.command in ("pause", "resume"):
            aliases = _sonos_zone_aliases(self._sonos)
        elif ctx.command in ("open-door", "close-door", "is-open"):
            aliases = _tailwind_door_aliases(self._tailwind)
        elif ctx.command in ("set-display-name", "clear-display-name"):
            aliases = _all_cli_device_labels(self._kasa, self._tailwind)
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


async def _cmd_loop(
    kasa_mgr: KasaDeviceManager,
    sonos_mgr: SonosDeviceManager | None,
    tailwind_mgr: GotailwindDeviceManager | None,
    *,
    cache_path: Path | None,
    editing_mode: EditingMode,
    theme: _Theme,
) -> None:
    session = PromptSession(
        completer=_ReplCompleter(
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

        if cmd == "set-display-name":
            if cache_path is None:
                print(
                    theme.err(
                        "Persistence disabled; omit --no-discovery-cache to save display names."
                    ),
                    file=sys.stderr,
                )
                continue
            tokens = arg.split()
            triples = _collect_label_triples(kasa_mgr, tailwind_mgr)
            got = _greedy_resolve_set_display_tokens(tokens, triples)
            if got is None:
                print(
                    theme.err("Usage: set-display-name <device> <display name>"),
                    file=sys.stderr,
                )
                continue
            (backend, api_lookup_id), disp_name = got
            ck = _sqlite_canonical_key(
                backend, api_lookup_id, kasa_mgr, tailwind_mgr
            )
            if ck is None:
                print(theme.err("Could not resolve device for persistence."), file=sys.stderr)
                continue
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
            except ValueError:
                print(theme.err("Device not found after resolve."), file=sys.stderr)
                continue
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
            continue

        if cmd == "clear-display-name":
            if cache_path is None:
                print(
                    theme.err(
                        "Persistence disabled; omit --no-discovery-cache."
                    ),
                    file=sys.stderr,
                )
                continue
            if not arg.strip():
                print(
                    theme.err("Usage: clear-display-name <device>"),
                    file=sys.stderr,
                )
                continue
            triples = _collect_label_triples(kasa_mgr, tailwind_mgr)
            api_lookup_id, amb, meta = _resolve_cli_target(arg.strip(), triples)
            if api_lookup_id is None or meta is None:
                _report_resolve_failure(theme, "device", arg.strip(), amb)
                continue
            backend, _api = meta
            ck = _sqlite_canonical_key(
                backend, api_lookup_id, kasa_mgr, tailwind_mgr
            )
            if ck is None:
                print(theme.err("Could not resolve device."), file=sys.stderr)
                continue
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
            except (NotInitializedError, ValueError):
                pass
            print(
                f"{theme.dim('Cleared display name for')} "
                f"{theme.device(repr(ck))} {theme.dim('(' + backend + ')')}"
            )
            continue

        if cmd == "show-devices":
            print(theme.header("Kasa switches:"))
            try:
                rows = sorted(
                    kasa_mgr.switches, key=lambda s: s.preferred_label.lower()
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
                    players = list(sonos_mgr.players)
                    if not players:
                        print(theme.dim("  (none discovered)"))
                    for p in players:
                        playback = await asyncio.to_thread(p.transport_state_summary)
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
                    doors = list(tailwind_mgr.doors)
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
            continue

        if cmd == "refresh":
            await kasa_mgr.disconnect()
            if sonos_mgr is not None:
                await sonos_mgr.disconnect()
            if tailwind_mgr is not None:
                await tailwind_mgr.disconnect()
            try:
                await kasa_mgr.fetch()
            except Exception as ex:
                print(theme.err(f"Kasa refresh failed: {ex}"), file=sys.stderr)
            if sonos_mgr is not None:
                try:
                    await sonos_mgr.fetch()
                except Exception as ex:
                    print(theme.err(f"Sonos refresh failed: {ex}"), file=sys.stderr)
            if tailwind_mgr is not None:
                try:
                    await tailwind_mgr.fetch()
                    if cache_path is not None and tailwind_mgr.host:
                        kasa_discovery_store.save_tailwind_host(
                            cache_path, tailwind_mgr.host
                        )
                except Exception as ex:
                    print(theme.err(f"Tailwind refresh failed: {ex}"), file=sys.stderr)
            nk = len(_kasa_switch_aliases(kasa_mgr))
            nz = _sonos_zone_count(sonos_mgr)
            nd = _tailwind_door_count(tailwind_mgr)
            tail = (
                f"({nk} Kasa switch(es), {nz} Sonos zone(s), {nd} Tailwind door(s))."
            )
            print(f"{theme.ok('Refreshed')} {theme.dim(tail)}")
            continue

        if cmd == "refresh-discovery":
            try:
                await kasa_mgr.rediscover()
            except Exception as ex:
                print(theme.err(f"Kasa discovery refresh failed: {ex}"), file=sys.stderr)
            if sonos_mgr is not None:
                try:
                    await sonos_mgr.rediscover()
                except Exception as ex:
                    print(
                        theme.err(f"Sonos discovery refresh failed: {ex}"),
                        file=sys.stderr,
                    )
            if tailwind_mgr is not None:
                try:
                    await tailwind_mgr.rediscover()
                    if cache_path is not None and tailwind_mgr.host:
                        kasa_discovery_store.save_tailwind_host(
                            cache_path, tailwind_mgr.host
                        )
                except Exception as ex:
                    print(
                        theme.err(f"Tailwind discovery refresh failed: {ex}"),
                        file=sys.stderr,
                    )
            nk = len(_kasa_switch_aliases(kasa_mgr))
            nz = _sonos_zone_count(sonos_mgr)
            nd = _tailwind_door_count(tailwind_mgr)
            tail = (
                f"({nk} Kasa switch(es), {nz} Sonos zone(s), {nd} Tailwind door(s))."
            )
            print(f"{theme.ok('Discovery refreshed')} {theme.dim(tail)}")
            continue

        if not arg:
            print(theme.err(f"{cmd} requires a device name."), file=sys.stderr)
            continue

        try:
            if cmd == "turn-on":
                key, amb = _resolve_device_name(arg, _kasa_switch_aliases(kasa_mgr))
                if key is None:
                    _report_resolve_failure(theme, "Kasa switch", arg, amb)
                    continue
                await kasa_mgr.turn_on(key)
                print(
                    f"{theme.device(repr(key))} {theme.dim('->')} {theme.ok('on')}"
                )
            elif cmd == "turn-off":
                key, amb = _resolve_device_name(arg, _kasa_switch_aliases(kasa_mgr))
                if key is None:
                    _report_resolve_failure(theme, "Kasa switch", arg, amb)
                    continue
                await kasa_mgr.turn_off(key)
                print(
                    f"{theme.device(repr(key))} {theme.dim('->')} "
                    f"{theme.meta('off')}"
                )
            elif cmd == "is-on":
                key, amb = _resolve_device_name(arg, _kasa_switch_aliases(kasa_mgr))
                if key is None:
                    _report_resolve_failure(theme, "Kasa switch", arg, amb)
                    continue
                on = await kasa_mgr.is_on(key)
                state = "on" if on else "off"
                st_fn = theme.ok if on else theme.meta
                print(
                    f"{theme.device(repr(key))} {theme.dim('->')} {st_fn(state)}"
                )
            elif cmd == "open-door":
                if tailwind_mgr is None:
                    print(theme.err("Tailwind not configured."), file=sys.stderr)
                    continue
                key, amb = _resolve_device_name(arg, _tailwind_door_aliases(tailwind_mgr))
                if key is None:
                    _report_resolve_failure(theme, "Tailwind door", arg, amb)
                    continue
                await tailwind_mgr.open(key)
                print(
                    f"{theme.device(repr(key))} {theme.dim('->')} "
                    f"{theme.ok('open')} {theme.dim('(command sent)')}"
                )
            elif cmd == "close-door":
                if tailwind_mgr is None:
                    print(theme.err("Tailwind not configured."), file=sys.stderr)
                    continue
                key, amb = _resolve_device_name(arg, _tailwind_door_aliases(tailwind_mgr))
                if key is None:
                    _report_resolve_failure(theme, "Tailwind door", arg, amb)
                    continue
                await tailwind_mgr.close(key)
                print(
                    f"{theme.device(repr(key))} {theme.dim('->')} "
                    f"{theme.meta('close')} {theme.dim('(command sent)')}"
                )
            elif cmd == "is-open":
                if tailwind_mgr is None:
                    print(theme.err("Tailwind not configured."), file=sys.stderr)
                    continue
                key, amb = _resolve_device_name(arg, _tailwind_door_aliases(tailwind_mgr))
                if key is None:
                    _report_resolve_failure(theme, "Tailwind door", arg, amb)
                    continue
                open_ = await tailwind_mgr.is_open(key)
                label = "open" if open_ else "closed"
                st_fn = theme.ok if open_ else theme.meta
                print(
                    f"{theme.device(repr(key))} {theme.dim('->')} {st_fn(label)}"
                )
            elif cmd == "pause":
                if sonos_mgr is None:
                    print(theme.err("Sonos not configured."), file=sys.stderr)
                    continue
                key, amb = _resolve_device_name(arg, _sonos_zone_aliases(sonos_mgr))
                if key is None:
                    _report_resolve_failure(theme, "Sonos zone", arg, amb)
                    continue
                await sonos_mgr.pause(key)
                print(
                    f"{theme.device(repr(key))} {theme.dim('->')} {theme.meta('paused')}"
                )
            elif cmd == "resume":
                if sonos_mgr is None:
                    print(theme.err("Sonos not configured."), file=sys.stderr)
                    continue
                key, amb = _resolve_device_name(arg, _sonos_zone_aliases(sonos_mgr))
                if key is None:
                    _report_resolve_failure(theme, "Sonos zone", arg, amb)
                    continue
                await sonos_mgr.resume(key)
                print(
                    f"{theme.device(repr(key))} {theme.dim('->')} {theme.ok('resumed')}"
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


async def _bootstrap_tailwind(
    *,
    args: argparse.Namespace,
    cache_path: Path | None,
    theme: _Theme,
    token: str,
) -> GotailwindDeviceManager | None:
    """Try explicit/env host, then cached host, then mDNS; persist host after success."""
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
            return mgr
        except BaseException as ex:
            last_exc = ex
            await mgr.disconnect()

    if last_exc is not None:
        print(theme.err(f"GoTailwind discovery failed: {last_exc}"), file=sys.stderr)
    return None


async def _async_main(args: argparse.Namespace) -> None:
    cache_path = Path(args.discovery_cache).expanduser().resolve() if args.discovery_cache else None
    theme = _Theme(enabled=_stdout_color_enabled(args.color))
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

    print(f"{theme.warn('Discovering Kasa devices…')}", flush=True)
    kasa_ok = False
    try:
        await kasa_mgr.fetch()
        kasa_ok = True
    except Exception as ex:
        print(theme.err(f"Kasa discovery failed: {ex}"), file=sys.stderr)

    tw_ok = False
    tailwind_mgr: GotailwindDeviceManager | None = None
    if token:
        print(f"{theme.warn('Discovering GoTailwind doors…')}", flush=True)
        tailwind_mgr = await _bootstrap_tailwind(
            args=args,
            cache_path=cache_path,
            theme=theme,
            token=token,
        )
        tw_ok = tailwind_mgr is not None
    else:
        print(
            f"{theme.dim('GoTailwind skipped (no token).')} "
            f"{theme.warn('Set TAILWIND_TOKEN or --tailwind-token.')}",
            flush=True,
        )

    sonos_mgr: SonosDeviceManager | None = None
    if args.no_sonos:
        print(f"{theme.dim('Sonos skipped (--no-sonos).')}", flush=True)
    else:
        print(f"{theme.warn('Discovering Sonos zones…')}", flush=True)
        sonos_mgr = SonosDeviceManager(
            discovery_timeout=float(args.sonos_discovery_timeout),
        )
        try:
            await sonos_mgr.fetch()
        except Exception as ex:
            print(theme.err(f"Sonos discovery failed: {ex}"), file=sys.stderr)
            await sonos_mgr.disconnect()
            sonos_mgr = None

    sonos_ready = sonos_mgr is not None
    if not kasa_ok and not tw_ok and not sonos_ready:
        print(theme.err("No backends initialized; exiting."), file=sys.stderr)
        raise SystemExit(1)

    ns = len(_kasa_switch_aliases(kasa_mgr))
    nz = _sonos_zone_count(sonos_mgr)
    nd = _tailwind_door_count(tailwind_mgr)
    tail = (
        f"({ns} Kasa switch(es), {nz} Sonos zone(s), {nd} Tailwind door(s)). "
        "Tab-complete commands and names."
    )
    print(f"{theme.ok('Ready')} {theme.dim(tail)}", flush=True)
    try:
        await _cmd_loop(
            kasa_mgr,
            sonos_mgr,
            tailwind_mgr,
            cache_path=cache_path,
            editing_mode=_editing_mode_enum(args.edit_mode),
            theme=theme,
        )
    finally:
        await kasa_mgr.disconnect()
        if sonos_mgr is not None:
            await sonos_mgr.disconnect()
        if tailwind_mgr is not None:
            await tailwind_mgr.disconnect()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Interactive REPL for Kasa switches, Sonos zones, and GoTailwind garage doors."
        ),
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
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
