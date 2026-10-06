"""``rotate-secrets`` REPL command: re-encrypt stored secrets, preview with ``--check``, local to the CLI."""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db.models import AppSecret
from app.db.secrets import load_tailwind_token_from_db, save_tailwind_token_to_db
from app.db.session import discovery_session
from app.domesti_bot_cli import _repl_cmd_rotate_secrets, _secrets_file_key_count, _Theme, execute_line_for_api

_TOKEN = "-".join(["SENTINEL", "repl", "token"])


def _key() -> str:
    return Fernet.generate_key().decode("ascii")


def _run(arg: str, db: Path | None) -> str:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        _repl_cmd_rotate_secrets(arg=arg, cache_path=db, theme=_Theme(enabled=False))
    return out.getvalue() + err.getvalue()


def _ciphertext(db: Path) -> bytes:
    with discovery_session(db) as session:
        return bytes(session.scalars(select(AppSecret.ciphertext).where(AppSecret.key == "tailwind_token")).one())


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str, Path]:
    old, new = _key(), _key()
    db = tmp_path / "ui.sqlite"
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", old)
    save_tailwind_token_to_db(db, _TOKEN)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{new},{old}")
    return old, new, db


def test_check_previews_without_writing(keys: tuple[str, str, Path]) -> None:
    _old, _new, db = keys
    before = _ciphertext(db)
    output = _run("--check", db)
    assert "would re-encrypt 1 secret(s); 0 already on the newest key" in output
    assert _ciphertext(db) == before


def test_rotate_reencrypts_and_says_the_old_key_can_go(
    keys: tuple[str, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, new, db = keys
    output = _run("", db)
    assert "re-encrypted 1 secret(s); 0 already on the newest key" in output
    assert "older keys can now be removed" in output
    assert _TOKEN not in output
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", new)
    assert load_tailwind_token_from_db(db) == _TOKEN


def test_second_run_reports_nothing_left_to_do(keys: tuple[str, str, Path]) -> None:
    _old, _new, db = keys
    _run("", db)
    output = _run("", db)
    assert "re-encrypted 0 secret(s); 1 already on the newest key" in output
    assert "older keys can now be removed" not in output


def test_undecryptable_rows_abort_unless_skipped(keys: tuple[str, str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _old, new, db = keys
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", _key())
    save_tailwind_token_to_db(db, "other")  # now under a key we will not list
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{new},{_old}")
    before = _ciphertext(db)

    aborted = _run("", db)
    assert "rotate-secrets:" in aborted and "tailwind_token" in aborted and "nothing was changed" in aborted
    assert _ciphertext(db) == before

    skipped = _run("--skip-undecryptable", db)
    assert "no configured key decrypts 1 secret(s): tailwind_token" in skipped
    assert _ciphertext(db) == before


def test_unknown_option_and_missing_cache_are_reported(keys: tuple[str, str, Path]) -> None:
    _old, _new, db = keys
    assert "unknown option '--force'" in _run("--force", db)
    assert "needs the SQLite cache" in _run("", None)


def test_rotate_secrets_is_not_available_over_http(tmp_path: Path) -> None:
    import asyncio

    stdout, stderr, error = asyncio.run(
        execute_line_for_api(
            cast(Any, None),
            None,
            None,
            None,
            None,
            None,
            cache_path=tmp_path / "ui.sqlite",
            androidtv_zeroconf_timeout=1.0,
            ep1_zeroconf_timeout=1.0,
            line="rotate-secrets",
        )
    )
    assert (stdout, stderr) == ("", "")
    assert error == "rotate-secrets is local to the CLI session"


def test_a_busy_database_is_reported_as_retryable_not_a_traceback(
    keys: tuple[str, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, _new, db = keys
    before = _ciphertext(db)

    def _busy(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("UPDATE app_secrets", {}, Exception("database is locked"))

    monkeypatch.setattr("app.domesti_bot_cli.rotate_app_secrets", _busy)
    output = _run("", db)
    assert "the database was busy; nothing was changed, retry" in output
    assert _ciphertext(db) == before


def test_check_with_undecryptable_rows_lists_them_and_does_not_fail(
    keys: tuple[str, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, db = keys
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", _key())
    save_tailwind_token_to_db(db, "other")
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{new},{old}")
    before = _ciphertext(db)
    output = _run("--check", db)
    assert "no configured key decrypts 1 secret(s): tailwind_token" in output
    assert _ciphertext(db) == before


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (json.dumps({"domesti_secrets_key": "a,b"}), 2),
        (json.dumps({"domesti_secrets_key": ["a", " ", "b", "c"]}), 3),
        (json.dumps({"domesti_secrets_key": "a"}), 1),
        (json.dumps({}), 0),
        ("not json", 0),
        (json.dumps(["a"]), 0),
    ],
)
def test_secrets_file_key_count_ignores_the_environment_and_bad_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, expected: int
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", "x,y,z,w")
    path = tmp_path / "domesti-bot.config.json"
    path.write_text(content)
    assert _secrets_file_key_count(path) == expected


def test_a_non_lock_database_error_is_not_called_busy_and_leaks_no_sql(
    keys: tuple[str, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, _new, db = keys

    def _broken(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("UPDATE app_secrets SET ciphertext=?", {"c": b"CIPHER"}, Exception("disk I/O error"))

    monkeypatch.setattr("app.domesti_bot_cli.rotate_app_secrets", _broken)
    output = _run("", db)
    assert "database error (Exception); nothing was changed" in output
    assert "busy" not in output
    assert "UPDATE" not in output and "CIPHER" not in output


def test_check_on_a_missing_or_unbootstrapped_database_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", ",".join([_key(), _key()]))
    missing = tmp_path / "missing.sqlite"
    assert "would re-encrypt 0 secret(s); 0 already on the newest key" in _run("--check", missing)
    assert not missing.exists()

    bare = tmp_path / "bare.sqlite"
    sqlite3.connect(bare).close()
    before = bare.read_bytes()
    assert "would re-encrypt 0 secret(s)" in _run("--check", bare)
    assert bare.read_bytes() == before
    with sqlite3.connect(bare) as conn:
        assert conn.execute("select name from sqlite_master").fetchall() == []
