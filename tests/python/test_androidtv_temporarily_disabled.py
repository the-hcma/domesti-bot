"""Lock the contract for the temporary Google Cast disable.

The constant in :mod:`app.androidtv_device_manager` is intentionally
load-bearing: every call site that *would* have spun up a Cast manager
(bootstrap, REPL refresh, web UI tile rendering) routes through it
once. The test asserts:

* The constant exists, is ``True``, and the human-readable reason
  string mentions the ``TODO(google-cast-on-off)`` marker so a
  ``rg`` for that string finds every disabled call site at once.
* ``app.domesti_bot_cli`` re-exports the same flag so the bootstrap
  branch can be verified by importing from one place.
* The ``--no-androidtv`` CLI flag's help text references the gate
  so users running ``--help`` don't think the flag controls
  whether Cast comes up.

When the on/off path is investigated and the gate is removed, this
test will fail — that is intentional. Delete the file as part of the
re-enable PR.
"""

from __future__ import annotations

from app import androidtv_device_manager
from app import domesti_bot_cli


def test_androidtv_disabled_constant_is_true() -> None:
    assert androidtv_device_manager.ANDROIDTV_TEMPORARILY_DISABLED is True


def test_androidtv_disabled_reason_mentions_todo_marker() -> None:
    reason = androidtv_device_manager.ANDROIDTV_TEMPORARILY_DISABLED_REASON
    assert "google-cast-on-off" in reason, reason
    assert "temporarily disabled" in reason.lower(), reason


def test_domesti_bot_cli_reexports_the_disable_flag() -> None:
    # The bootstrap branch in ``boot_androidtv`` checks this re-export,
    # so a future move of the constant must keep the symbol reachable
    # from ``app.domesti_bot_cli`` or risk a silent fall-through.
    assert (
        domesti_bot_cli.ANDROIDTV_TEMPORARILY_DISABLED
        is androidtv_device_manager.ANDROIDTV_TEMPORARILY_DISABLED
    )
    assert (
        domesti_bot_cli.ANDROIDTV_TEMPORARILY_DISABLED_REASON
        is androidtv_device_manager.ANDROIDTV_TEMPORARILY_DISABLED_REASON
    )


def test_cli_help_text_for_no_androidtv_points_at_the_gate() -> None:
    parser = domesti_bot_cli.build_arg_parser()
    actions = {a.dest: a for a in parser._actions}
    assert "no_androidtv" in actions, "--no-androidtv option missing"
    help_text = actions["no_androidtv"].help or ""
    assert "ANDROIDTV_TEMPORARILY_DISABLED" in help_text, help_text
    assert "google-cast-on-off" in help_text, help_text
