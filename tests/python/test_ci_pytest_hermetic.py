"""Tests for ``.github/ci/pytest-hermetic`` and its worker-crash retry (issue #936).

The script is run for real with a stub ``uv`` first on ``PATH``. The stub answers the
calls ``setup-python`` makes and replays scripted output and exit codes for each
``uv run pytest`` call, so no real pytest run, network access or fixed delay is involved.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ".github/ci/pytest-hermetic"

_CRASH_ID = "tests/python/test_api_ui_state.py::test_get_v1_ui_state_rejects_request"
# Matches real pytest output: the summary line truncates its message to the terminal width.
_CRASH_LINES = (
    "[gw2] node down: Not properly terminated\n"
    f"worker 'gw2' crashed while running '{_CRASH_ID}'\n"
    f"FAILED {_CRASH_ID} - worker 'gw2' c...\n"
)

_STUB_UV = """\
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  python | sync) exit 0 ;;
  run)
    shift
    if [[ "$1" == python ]]; then
      echo "$STUB_PYTHON_VERSION"
      exit 0
    fi
    if [[ "$1" == pytest ]]; then
      echo "$*" >> "$STUB_DIR/calls"
      n="$(wc -l < "$STUB_DIR/calls")"
      cat "$STUB_DIR/out-$n"
      exit "$(cat "$STUB_DIR/rc-$n")"
    fi
    ;;
esac
echo "unexpected uv call: $*" >&2
exit 99
"""


@dataclass(frozen=True)
class _Run:
    calls: list[str]
    returncode: int
    stderr: str
    stdout: str


def _run_script(
    tmp_path: Path,
    runs: list[tuple[int, str]],
    env_overrides: dict[str, str | None] | None = None,
) -> _Run:
    """Run the script with a stub ``uv`` that replays ``runs`` as (exit code, output)."""

    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "calls").write_text("")
    for number, (returncode, output) in enumerate(runs, start=1):
        (stub_dir / f"rc-{number}").write_text(str(returncode))
        (stub_dir / f"out-{number}").write_text(output)
    uv = stub_dir / "uv"
    uv.write_text(_STUB_UV)
    uv.chmod(0o755)
    python_version = ".".join((_REPO_ROOT / ".python-version").read_text().strip().split(".")[:2])
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_DIR": str(stub_dir),
        "STUB_PYTHON_VERSION": python_version,
        "TMPDIR": str(tmp_path),
        # The wrapper re-execs through with-tmp-layout unless this is set; keep the test independent of
        # any repository-helpers clone on the host. The re-exec branch has its own tests below.
        "CI_TMP_LAYOUT": "1",
    }
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        ["bash", _SCRIPT],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    calls = (stub_dir / "calls").read_text().splitlines()
    return _Run(calls=calls, returncode=result.returncode, stderr=result.stderr, stdout=result.stdout)


def test_crashed_test_id_containing_a_spaced_hyphen_is_rerun_whole(tmp_path: Path) -> None:
    crash_id = "tests/python/test_x.py::test_y[a - b]"
    output = f"worker 'gw1' crashed while running '{crash_id}'\nFAILED {crash_id} - worker 'gw1' c...\n"
    run = _run_script(tmp_path, [(1, output), (0, "1 passed\n")])
    assert run.returncode == 0
    assert len(run.calls) == 2
    assert run.calls[1].endswith(crash_id)


def test_ordinary_failure_beside_a_spaced_hyphen_crash_is_not_retried(tmp_path: Path) -> None:
    crash_id = "tests/python/test_x.py::test_y[a - b]"
    output = (
        f"worker 'gw1' crashed while running '{crash_id}'\n"
        f"FAILED {crash_id} - worker 'gw1' c...\n"
        "FAILED tests/python/test_x.py::test_y[a] - assert 1 == 2\n"
    )
    run = _run_script(tmp_path, [(1, output), (0, "")])
    assert run.returncode == 1
    assert len(run.calls) == 1


def test_ordinary_failure_of_the_crashed_test_itself_is_not_retried(tmp_path: Path) -> None:
    output = (
        f"worker 'gw2' crashed while running '{_CRASH_ID}'\n"
        f"FAILED {_CRASH_ID} - assert 1 == 2\n"
        f"FAILED {_CRASH_ID} - worker 'gw2' c...\n"
    )
    run = _run_script(tmp_path, [(1, output), (0, "")])
    assert run.returncode == 1
    assert len(run.calls) == 1
    assert "besides the worker crash" in run.stdout


def test_crash_summary_line_trimmed_to_nothing_is_still_rerun(tmp_path: Path) -> None:
    for tail in ("", " - ...", " - worker 'gw2' c..."):
        output = f"worker 'gw2' crashed while running '{_CRASH_ID}'\nFAILED {_CRASH_ID}{tail}\n"
        case_dir = tmp_path / f"case{len(tail)}"
        case_dir.mkdir()
        run = _run_script(case_dir, [(1, output), (0, "1 passed\n")])
        assert run.returncode == 0, tail
        assert len(run.calls) == 2, tail


def test_crash_alongside_ordinary_failure_is_not_retried(tmp_path: Path) -> None:
    other = "FAILED tests/python/test_other.py::test_real_bug - assert 1 == 2\n"
    run = _run_script(tmp_path, [(1, _CRASH_LINES + other), (0, "")])
    assert run.returncode == 1
    assert len(run.calls) == 1
    assert "besides the worker crash" in run.stdout


def test_crash_signature_without_node_id_is_not_retried(tmp_path: Path) -> None:
    run = _run_script(tmp_path, [(1, "[gw2] node down: Not properly terminated\n"), (0, "")])
    assert run.returncode == 1
    assert len(run.calls) == 1


def test_crashed_test_that_fails_again_keeps_the_job_red(tmp_path: Path) -> None:
    run = _run_script(tmp_path, [(1, _CRASH_LINES), (1, f"FAILED {_CRASH_ID} - assert False\n")])
    assert run.returncode == 1
    assert len(run.calls) == 2


def test_crashed_test_is_rerun_once_by_node_id_and_passes(tmp_path: Path) -> None:
    run = _run_script(tmp_path, [(1, _CRASH_LINES), (0, "1 passed\n")])
    assert run.returncode == 0
    assert len(run.calls) == 2
    assert run.calls[1].endswith(_CRASH_ID)
    assert "-n 0" in run.calls[1]
    assert "--last-failed" not in run.calls[1]
    assert "::warning title=pytest worker crash::the rerun passed" in run.stdout
    assert f"rerunning once: {_CRASH_ID}" in run.stdout


def test_green_run_is_not_retried(tmp_path: Path) -> None:
    run = _run_script(tmp_path, [(0, "2579 passed\n")])
    assert run.returncode == 0
    assert len(run.calls) == 1


def test_ordinary_failure_is_not_retried(tmp_path: Path) -> None:
    run = _run_script(tmp_path, [(1, "FAILED tests/python/test_other.py::test_real_bug - x\n")])
    assert run.returncode == 1
    assert len(run.calls) == 1


def _without_helpers(tmp_path: Path, **extra: str | None) -> dict[str, str | None]:
    """Environment with the layout re-exec enabled and no repository-helpers clone to find."""

    empty = tmp_path / "no-helpers"
    empty.mkdir()
    return {"CI_TMP_LAYOUT": None, "HOME": str(empty), "REPOSITORY_HELPERS_DIR": str(empty), **extra}


def test_ci_without_the_layout_helper_fails_with_a_pointer_to_the_action(tmp_path: Path) -> None:
    run = _run_script(tmp_path, [(0, "")], _without_helpers(tmp_path, GITHUB_ACTIONS="true"))
    assert run.returncode == 1
    assert "setup-tmp-layout" in run.stderr
    assert run.calls == []


def test_a_host_without_the_layout_helper_runs_the_tests_unchanged(tmp_path: Path) -> None:
    run = _run_script(tmp_path, [(0, "1 passed\n")], _without_helpers(tmp_path, GITHUB_ACTIONS=None))
    assert run.returncode == 0
    assert len(run.calls) == 1


def test_the_layout_helper_wraps_the_run_in_the_uv_and_pytest_layouts(tmp_path: Path) -> None:
    helpers = tmp_path / "helpers"
    (helpers / "scripts" / "dev").mkdir(parents=True)
    fake = helpers / "scripts" / "dev" / "with-tmp-layout"
    fake.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$1" >> "$TMPDIR/layout-tools"\nshift 2\nexec "$@"\n')
    fake.chmod(0o755)
    env = _without_helpers(tmp_path, GITHUB_ACTIONS=None, REPOSITORY_HELPERS_DIR=str(helpers))
    run = _run_script(tmp_path, [(0, "1 passed\n")], env)
    assert run.returncode == 0
    assert (tmp_path / "layout-tools").read_text().split() == ["uv", "pytest"]
