"""Console script ``--version`` flags for pip/pipx entry points."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import app.build_info as build_info
from app.domesti_bot_cli import build_arg_parser
from config.serve import build_serve_parser


def test_build_arg_parser_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_arg_parser().parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("domesti-bot ")
    assert "(" in out and ")" in out


def test_build_serve_parser_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_serve_parser().parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("domesti-bot-server ")


def test_format_cli_version_line_uses_build_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build_info,
        "get_build_info",
        lambda: ("1.2.3", "deadbeefcafe"),
    )
    line = build_info.format_cli_version_line(prog="domesti-bot")
    assert line == "domesti-bot 1.2.3 (deadbeefcafe)"


def test_pyproject_declares_console_scripts() -> None:
    data = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8",
        ),
    )
    scripts = data["project"]["scripts"]
    assert scripts["domesti-bot"] == "app.domesti_bot_cli:main"
    assert scripts["domesti-bot-server"] == "config.serve:main"
