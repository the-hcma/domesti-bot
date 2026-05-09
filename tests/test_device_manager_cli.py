"""Tests for :mod:`device_manager_cli` parsing (no hardware)."""

from __future__ import annotations

import pytest

from device_manager_cli import (
    _ArgCtx,
    _CmdCtx,
    _COMMAND_HELP_LINES,
    _collect_label_triples,
    _greedy_resolve_set_display_tokens,
    _normalize_edit_mode_choice,
    _parse_completion_buffer,
    _resolve_cli_target,
    _resolve_device_name,
    COMMANDS,
    build_arg_parser,
    split_invocation,
)


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

    assert _collect_label_triples(_EmptyKasa(), None) == []
