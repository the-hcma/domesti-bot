---
description: Block PR submit until pre-pr-checks pass; format before check; no truncated output
alwaysApply: true
---

# Pre-PR checks (required)

`pre-pr-checks` lives in **repository-helpers**, not in this repo. Resolve the
clone once:

```bash
rh="${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}"
```

`pre-pr-checks` resolves its target from `$PWD`, so it audits **this** repo when
run from a feature worktree here. The `submit-stack` wrapper does **not** — it
resolves the repo from its own path and would submit the repository-helpers
clone, so consumers submit with a bare `gh stack submit` / `gt submit`.

Before submitting a PR:

1. Run **`"${rh}/scripts/dev/pre-pr-checks"`** from this repo's feature worktree
   (must exit 0), then submit with bare **`gh stack submit --auto`** or
   **`gt submit`** per this repo's `.github/stacking-tool` marker, from the same
   worktree. Do **not** use `"${rh}/scripts/dev/submit-stack"` from here.

2. Do **not** submit if pre-pr-checks failed or was skipped. A skipped job is
   allowed **only** when the user has approved it for this PR: pass
   `PRE_PR_CHECKS_SKIP=job1,job2` (never a silent skip) and record the skipped
   jobs and the reason in the PR **Test plan**.

3. In the PR **Test plan**, note that `"${rh}/scripts/dev/pre-pr-checks"` passed
   (paste the final `==> pre-pr-checks passed` line from the full run).

4. Scripts must not leave changes on the **primary (main) worktree**.

## Apply formatters before check (required)

`pre-pr-checks` is **check-only** by default (e.g. `ruff format --check`, `cargo fmt -- --check`).
After review-fix edits — especially string literals — **apply** formatters first, then run the gate:

```bash
# Python (match CI paths; adjust for the repo)
uv run ruff format .
# Rust
cargo fmt --all
# Then the gate
"${rh}/scripts/dev/pre-pr-checks"
# Or apply + check in one shot (mutates the worktree):
"${rh}/scripts/dev/pre-pr-checks" --fix
```

Commit any format-only diff before submit. Do **not** treat a green `pytest` / `ruff check` /
partial job as a pre-PR pass.

## No truncated pre-PR output

Do **not** pipe `pre-pr-checks` to `tail` / `head`. Require exit **0** and the final
`==> pre-pr-checks passed` line from the full run.

```bash
# ❌ Truncated — hides failures above the last few lines
"${rh}/scripts/dev/pre-pr-checks" 2>&1 | tail -8

# ✅ Full output + exit status
"${rh}/scripts/dev/pre-pr-checks"
```
