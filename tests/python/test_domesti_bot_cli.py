"""Tests for :mod:`domesti_bot_cli` parsing (no hardware)."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from prompt_toolkit.formatted_text import to_plain_text

from app.androidtv_device_manager import AndroidTvDeviceManager
from app.device_completion import CompletionAlias
from app.device_display import format_device_display
from app.domesti_bot_cli import (
    _COMMAND_HELP_LINES,
    COMMANDS,
    COMPLETION_DISCOVERING_HINT,
    DISCOVERY_IN_PROGRESS_MSG,
    FamilyDiscoveryStatus,
    _ArgCtx,
    _async_main,
    _CliDiscoverySession,
    _CmdCtx,
    _collect_label_triples,
    _greedy_resolve_set_display_tokens,
    _kasa_switch_aliases,
    _normalize_edit_mode_choice,
    _parse_completion_alias_list,
    _parse_completion_buffer,
    _print_family_parallel_line,
    _repl_cmd_setup_secrets,
    _ReplCompleter,
    _resolve_cli_target,
    _resolve_device_name,
    _Theme,
    build_arg_parser,
    dispatch_repl_action,
    split_invocation,
)
from app.kasa_device_manager import KasaDeviceManager


def test_parse_completion_alias_list_accepts_legacy_strings() -> None:
    items = _parse_completion_alias_list(["Porch lights", "  ", 3])
    assert items == [CompletionAlias(display="Porch lights", matches=())]


def test_parse_completion_alias_list_reads_structured_items() -> None:
    mac = "aa:bb:cc:dd:ee:10"
    display = format_device_display(mac, "Porch lights")
    items = _parse_completion_alias_list([{"display": display, "matches": [mac, "Porch lights"]}])
    assert items == [CompletionAlias(display=display, matches=(mac, "Porch lights"))]


@pytest.mark.parametrize(
    ("buf", "expected"),
    [
        ("", _CmdCtx(partial="")),
        ("show", _CmdCtx(partial="show")),
        ("show-devices", _ArgCtx("show-devices", "")),
        ("show-devices ", _ArgCtx("show-devices", "")),
        ("turn-on ", _ArgCtx("turn-on", "")),
        ("turn-on Bas", _ArgCtx("turn-on", "Bas")),
        ("turn-on Basement leds", _ArgCtx("turn-on", "Basement leds")),
        ("  turn-off  X", _ArgCtx("turn-off", "X")),
        ("is-open ", _ArgCtx("is-open", "")),
        ("is-open 0", _ArgCtx("is-open", "0")),
        ("open-door garage", _ArgCtx("open-door", "garage")),
        ("edit-mode ", _ArgCtx("edit-mode", "")),
        ("edit-mode em", _ArgCtx("edit-mode", "em")),
        ("is-o", _CmdCtx(partial="is-o")),
    ],
)
def test_parse_completion_buffer(buf: str, expected: _CmdCtx | _ArgCtx) -> None:
    assert _parse_completion_buffer(buf) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "vim"),
        ("", "vim"),
        (" ", "vim"),
        ("emacs", "emacs"),
        ("e", "emacs"),
        ("vi", "vim"),
        ("vim", "vim"),
        ("v", "vim"),
        ("VI", "vim"),
        ("nano", "vim"),
    ],
)
def test_normalize_edit_mode_choice(raw: str | None, expected: str) -> None:
    assert _normalize_edit_mode_choice(raw) == expected


def test_command_help_lines_match_commands() -> None:
    mapped = {name for name, _ in _COMMAND_HELP_LINES}
    assert mapped == set(COMMANDS)
    assert len(_COMMAND_HELP_LINES) == len(COMMANDS)


def test_build_arg_parser_edit_mode_defaults_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVICE_MANAGER_EDIT_MODE", raising=False)
    args = build_arg_parser().parse_args([])
    assert args.edit_mode == "vim"

    monkeypatch.setenv("DEVICE_MANAGER_EDIT_MODE", "emacs")
    args = build_arg_parser().parse_args([])
    assert args.edit_mode == "emacs"

    monkeypatch.setenv("DEVICE_MANAGER_EDIT_MODE", "vim")
    args = build_arg_parser().parse_args([])
    assert args.edit_mode == "vim"

    monkeypatch.setenv("DEVICE_MANAGER_EDIT_MODE", "vim")
    args = build_arg_parser().parse_args(["--edit-mode", "emacs"])
    assert args.edit_mode == "emacs"


def test_split_invocation_known_commands() -> None:
    assert split_invocation("show-devices") == ("show-devices", "")
    assert split_invocation("edit-mode vim") == ("edit-mode", "vim")
    assert split_invocation("refresh-discovery") == ("refresh-discovery", "")
    assert split_invocation("turn-on Basement lamp") == ("turn-on", "Basement lamp")
    assert split_invocation("  is-on  Kitchen  ") == ("is-on", "Kitchen")
    assert split_invocation("is-open 0") == ("is-open", "0")
    assert split_invocation("close-door main") == ("close-door", "main")


def test_split_invocation_unknown() -> None:
    assert split_invocation("nope") is None
    assert split_invocation("") is None
    assert split_invocation("   ") is None


def test_resolve_device_name_case_insensitive_exact() -> None:
    cands = ["Basement lamp", "Kitchen"]
    assert _resolve_device_name("basement lamp", cands) == ("Basement lamp", [])
    assert _resolve_device_name("BASEMENT LAMP", cands) == ("Basement lamp", [])
    assert _resolve_device_name("Kitchen", cands) == ("Kitchen", [])


def test_resolve_device_name_unique_prefix() -> None:
    cands = ["Basement lamp", "Kitchen"]
    assert _resolve_device_name("base", cands) == ("Basement lamp", [])
    assert _resolve_device_name("KIT", cands) == ("Kitchen", [])


def test_resolve_device_name_ambiguous_prefix() -> None:
    cands = ["Basement lamp", "Basement leds"]
    key2, amb2 = _resolve_device_name("basement", cands)
    assert key2 is None
    assert set(amb2) == {"Basement lamp", "Basement leds"}


def test_resolve_device_name_no_match() -> None:
    assert _resolve_device_name("attic", ["Basement lamp"]) == (None, [])
    assert _resolve_device_name("", ["x"]) == (None, [])


def test_greedy_set_display_splits_device_and_name() -> None:
    triples = [
        ("Basement lamp", "kasa", "Basement lamp"),
        ("Kitchen", "kasa", "Kitchen"),
    ]
    got = _greedy_resolve_set_display_tokens(
        ["Basement", "lamp", "Main", "lights"],
        triples,
    )
    assert got == (("kasa", "Basement lamp"), "Main lights")


def test_resolve_cli_target_maps_preferred_label() -> None:
    triples = [
        ("hwalias", "kasa", "hwalias"),
        ("Pretty name", "kasa", "hwalias"),
    ]
    api, amb, meta = _resolve_cli_target("pretty name", triples)
    assert amb == []
    assert api == "hwalias"
    assert meta == ("kasa", "hwalias")


def test_collect_label_triples_empty_switches() -> None:
    class _EmptyKasa:
        switches = ()

    # The helper only reads ``.switches`` — duck-type with a stub and cast for pyright.
    assert _collect_label_triples(cast(KasaDeviceManager, _EmptyKasa()), None) == []


def test_print_family_parallel_line_annotates_cache_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    theme = _Theme(enabled=False)
    result = {
        "skipped": False,
        "exc": None,
        "ok": True,
        "source": "cache",
        "count": 9,
    }
    _print_family_parallel_line(theme, "kasa", result, ok_verb="ready")
    out = capsys.readouterr().out
    assert "Kasa: ready (cache, 9 switches)" in out


def test_print_family_parallel_line_annotates_lan_discovery_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    theme = _Theme(enabled=False)
    result = {
        "skipped": False,
        "exc": None,
        "ok": True,
        "source": "discovery",
        "count": 5,
    }
    _print_family_parallel_line(theme, "androidtv", result, ok_verb="ready")
    out = capsys.readouterr().out
    assert "Google Cast: ready (LAN discovery, 5 devices)" in out


def test_print_family_parallel_line_omits_source_when_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Tailwind has no LAN sweep; the renderer must not invent a label."""

    theme = _Theme(enabled=False)
    result = {
        "skipped": False,
        "exc": None,
        "ok": True,
        "source": None,
        "count": 2,
    }
    _print_family_parallel_line(theme, "gotailwind", result, ok_verb="ready")
    out = capsys.readouterr().out
    assert "GoTailwind: ready (2 doors)" in out
    assert "cache" not in out
    assert "LAN discovery" not in out


def test_print_family_parallel_line_falls_back_to_bare_ready_without_source_or_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Older bundles (no ``source``/``count`` keys) still render cleanly."""

    theme = _Theme(enabled=False)
    result = {
        "skipped": False,
        "exc": None,
        "ok": True,
    }
    _print_family_parallel_line(theme, "sonos", result, ok_verb="ready")
    out = capsys.readouterr().out
    assert out.strip() == "Sonos: ready"


@pytest.mark.asyncio
async def test_repl_setup_secrets_writes_json_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_file = tmp_path / "domesti-bot.config.json"
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(secrets_file))
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)

    async def prompt(_message: str, _is_password: bool) -> str:
        return ""

    await _repl_cmd_setup_secrets(prompt_fn=prompt, theme=_Theme(enabled=False))
    assert secrets_file.is_file()
    payload = json.loads(secrets_file.read_text(encoding="utf-8"))
    assert payload["domesti_secrets_key"]


@pytest.mark.asyncio
async def test_show_devices_lists_vizio_tvs() -> None:
    from contextlib import redirect_stdout
    from io import StringIO
    from unittest.mock import MagicMock

    from app.domesti_bot_cli import _repl_cmd_show_devices
    from app.vizio_device_manager import VizioTvDevice, VizioTvEndpoint

    endpoint = VizioTvEndpoint(
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
    )
    tv = VizioTvDevice(endpoint, MagicMock(), display_name="Kitchen TV", mac_address="00:bd:3e:d5:f0:11")
    tv.set_power(True)

    vizio_mgr = MagicMock()
    vizio_mgr.tvs = (tv,)

    kasa_mgr = MagicMock()
    kasa_mgr.switches = []

    out = StringIO()
    with redirect_stdout(out):
        await _repl_cmd_show_devices(
            kasa_mgr=kasa_mgr,
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=vizio_mgr,
            theme=_Theme(enabled=False),
        )
    text = out.getvalue()
    assert "Vizio TVs:" in text
    assert "Kitchen TV" in text
    assert "192.168.86.201" in text
    assert "MAC address:" in text
    assert "00:bd:3e:d5:f0:11" in text
    assert "(on)" in text


def test_kasa_switch_aliases_are_name_and_mac() -> None:
    mac = "aa:bb:cc:dd:ee:10"

    class _Switch:
        identifier = mac
        preferred_label = "Porch lights"

    class _Kasa:
        switches = (_Switch(),)

    aliases = _kasa_switch_aliases(cast(KasaDeviceManager, _Kasa()))
    assert aliases == [format_device_display(mac, "Porch lights")]
    assert mac not in aliases


def test_resolve_cli_target_accepts_formatted_display_and_mac() -> None:
    mac = "aa:bb:cc:dd:ee:10"
    display = format_device_display(mac, "Porch lights")
    triples = [
        (mac, "kasa", mac),
        ("Porch lights", "kasa", mac),
        (display, "kasa", mac),
    ]
    api, amb, meta = _resolve_cli_target(display, triples)
    assert amb == []
    assert api == mac
    assert meta == ("kasa", mac)
    api2, amb2, meta2 = _resolve_cli_target(mac, triples)
    assert amb2 == []
    assert api2 == mac
    assert meta2 == ("kasa", mac)


def test_resolve_cli_target_same_device_prefix_is_not_ambiguous() -> None:
    mac = "aa:bb:cc:dd:ee:10"
    display = format_device_display(mac, "Porch lights")
    triples = [
        (mac, "kasa", mac),
        ("Porch lights", "kasa", mac),
        (display, "kasa", mac),
    ]
    api, amb, meta = _resolve_cli_target("Porch", triples)
    assert amb == []
    assert api == mac
    assert meta == ("kasa", mac)


def test_resolve_cli_target_shared_prefix_across_devices_is_ambiguous() -> None:
    mac1 = "aa:bb:cc:dd:ee:10"
    mac2 = "aa:bb:cc:dd:ee:11"
    display1 = format_device_display(mac1, "Porch lights")
    display2 = format_device_display(mac2, "Porch heater")
    triples = [
        (mac1, "kasa", mac1),
        ("Porch lights", "kasa", mac1),
        (display1, "kasa", mac1),
        (mac2, "kasa", mac2),
        ("Porch heater", "kasa", mac2),
        (display2, "kasa", mac2),
    ]
    api, amb, meta = _resolve_cli_target("Porch", triples)
    assert api is None
    assert meta is None
    assert amb == sorted({display1, display2})


def test_repl_completer_hints_when_any_gated_family_pending() -> None:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    args = build_arg_parser().parse_args(["--no-discovery-cache"])
    args.discovery_cache = None
    discovery = _CliDiscoverySession.from_args(args)
    discovery.family_status["androidtv"] = FamilyDiscoveryStatus.READY

    class _Switch:
        identifier = "aa:bb:cc:dd:ee:10"
        preferred_label = "Porch lights"

    class _Android:
        switches = (_Switch(),)

    discovery.androidtv_mgr = cast(AndroidTvDeviceManager, _Android())
    completer = _ReplCompleter(
        androidtv=discovery.androidtv_mgr,
        kasa=discovery.kasa_mgr,
        sonos=None,
        tailwind=None,
        theme=_Theme(enabled=False),
        discovery=discovery,
    )
    completions = list(completer.get_completions(Document("turn-off ", 9), CompleteEvent()))
    assert len(completions) == 1
    assert to_plain_text(completions[0].display) == COMPLETION_DISCOVERING_HINT


def test_repl_completer_inserts_name_and_mac_for_mac_prefix() -> None:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    mac = "aa:bb:cc:dd:ee:10"
    display = format_device_display(mac, "Porch lights")

    class _Switch:
        identifier = mac
        preferred_label = "Porch lights"

    class _Kasa:
        switches = (_Switch(),)

    completer = _ReplCompleter(
        androidtv=None,
        kasa=cast(KasaDeviceManager, _Kasa()),
        sonos=None,
        tailwind=None,
        theme=_Theme(enabled=False),
    )
    completions = list(completer.get_completions(Document("turn-off aa:bb", 14), CompleteEvent()))
    assert [c.text for c in completions] == [display]


def test_repl_completer_shows_discovering_hint_when_family_pending() -> None:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    args = build_arg_parser().parse_args(["--no-discovery-cache"])
    args.discovery_cache = None
    discovery = _CliDiscoverySession.from_args(args)
    assert discovery.family_status["kasa"] is FamilyDiscoveryStatus.PENDING

    completer = _ReplCompleter(
        androidtv=None,
        kasa=discovery.kasa_mgr,
        sonos=None,
        tailwind=None,
        theme=_Theme(enabled=False),
        discovery=discovery,
    )
    completions = list(completer.get_completions(Document("turn-off ", 9), CompleteEvent()))
    assert len(completions) == 1
    assert to_plain_text(completions[0].display) == COMPLETION_DISCOVERING_HINT


@pytest.mark.asyncio
async def test_dispatch_prints_discovery_in_progress_while_family_pending() -> None:
    from contextlib import redirect_stderr
    from io import StringIO

    args = build_arg_parser().parse_args(["--no-discovery-cache"])
    args.discovery_cache = None
    discovery = _CliDiscoverySession.from_args(args)
    err = StringIO()
    with redirect_stderr(err):
        await dispatch_repl_action(
            discovery.kasa_mgr,
            None,
            None,
            None,
            None,
            None,
            cache_path=None,
            androidtv_zeroconf_timeout=1.0,
            ep1_zeroconf_timeout=1.0,
            theme=_Theme(enabled=False),
            cmd="turn-off",
            arg="Porch",
            discovery=discovery,
        )
    assert DISCOVERY_IN_PROGRESS_MSG in err.getvalue()


@pytest.mark.asyncio
async def test_refresh_prints_ep1_skipped_when_not_loaded() -> None:
    from contextlib import redirect_stdout
    from io import StringIO
    from unittest.mock import AsyncMock, MagicMock

    kasa = MagicMock()
    kasa.disconnect = AsyncMock()
    kasa.fetch = AsyncMock()
    kasa.switches = ()
    out = StringIO()
    with redirect_stdout(out):
        await dispatch_repl_action(
            cast(KasaDeviceManager, kasa),
            None,
            None,
            None,
            None,
            None,
            cache_path=None,
            androidtv_zeroconf_timeout=1.0,
            ep1_zeroconf_timeout=1.0,
            theme=_Theme(enabled=False),
            cmd="refresh",
            arg="",
        )
    text = out.getvalue()
    assert "Everything Presence One: skipped — not loaded" in text
    assert "Refreshed" in text


@pytest.mark.asyncio
async def test_async_main_starts_prompt_before_discovery_finishes() -> None:
    prompt_started = asyncio.Event()

    async def _hanging_bootstrap(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    async def _fake_cmd_loop(*_args: object, **_kwargs: object) -> None:
        prompt_started.set()
        await asyncio.Event().wait()

    async def _noop_shutdown(_state: object) -> None:
        return None

    args = build_arg_parser().parse_args(["--no-discovery-cache"])
    args.discovery_cache = None
    with (
        patch("app.domesti_bot_cli.bootstrap_device_managers", _hanging_bootstrap),
        patch("app.domesti_bot_cli._cmd_loop", _fake_cmd_loop),
        patch("app.domesti_bot_cli.shutdown_device_managers", _noop_shutdown),
    ):
        task = asyncio.create_task(_async_main(args))
        try:
            await asyncio.wait_for(prompt_started.wait(), timeout=2.0)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_async_main_requests_kasa_vendor_alias_sync() -> None:
    seen: dict[str, object] = {}
    bootstrap_started = asyncio.Event()

    async def _capture_bootstrap(*_args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        bootstrap_started.set()

    async def _fake_cmd_loop(*_args: object, **_kwargs: object) -> None:
        await bootstrap_started.wait()

    async def _noop_shutdown(_state: object) -> None:
        return None

    args = build_arg_parser().parse_args(["--no-discovery-cache"])
    args.discovery_cache = None
    with (
        patch("app.domesti_bot_cli.bootstrap_device_managers", _capture_bootstrap),
        patch("app.domesti_bot_cli._cmd_loop", _fake_cmd_loop),
        patch("app.domesti_bot_cli.shutdown_device_managers", _noop_shutdown),
    ):
        await _async_main(args)
    assert seen.get("sync_kasa_vendor_aliases") is True
    assert seen.get("exit_if_empty") is False
