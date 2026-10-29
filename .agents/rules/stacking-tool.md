---
description: Stack PRs with GitHub Stacked PRs (gh-stack) from .github/stacking-tool
alwaysApply: true
---

# Stacking tool preference

Before creating branches or submitting/restacking PRs, **read** `.github/stacking-tool` (single line: `gh-stack`). Graphite is retired org-wide (its merge queue builds unsigned commits, repository-helpers#772): a `graphite` marker is refused by the tooling, so migrate it to `gh-stack` first. Missing or invalid values: **stop and ask** — do not guess.

<!-- stacking-tool-canonical-gh-stack-skill: https://github.com/the-hcma/repository-helpers/blob/main/.agents/skills/gh-stack/SKILL.md -->

The canonical skill lives in **repository-helpers** (not copied into this repo):

- **gh-stack:** https://github.com/the-hcma/repository-helpers/blob/main/.agents/skills/gh-stack/SKILL.md

Local clone (when `${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}` is synced):

- `${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}/.agents/skills/gh-stack/SKILL.md`

## `gh-stack`

- Follow the gh-stack skill (non-interactive: `view --json`, `submit --auto --open`, named `init`/`add`).
- Prefer repository-helpers `scripts/dev/submit-stack` / `scripts/dev/ship-and-review` when available (they dispatch via `scripts/lib/stacking-tool`). Run them from this repo's feature worktree: they submit the repository of the current directory.
- **Do not** use `gt` on any stack: Graphite is retired.

## Marker cutover checklist

When flipping `.github/stacking-tool` (or landing an MQ/`gh-stack` cutover PR):

1. Update `AGENTS.md` stacking/merge guidance to match the new marker: merge with `${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}/scripts/dev/merge-stack --pr <n>` from the consumer worktree (it uses `gh stack merge` for a stacked PR and the merge queue or auto-merge otherwise), only after the user explicitly confirms; do not use `merge-it` or run `gh pr merge` or `gh stack merge` by hand.
2. Rewrite `.agents/rules/pr-ship-and-review.md` submit block to the gh-stack variant with `github-repo-lint --apply-fix`, which renders the marker-aware template. Do not copy the raw template by hand (`${REPOSITORY_HELPERS_DIR}/scripts/lib/repo-practices-agents/rules/pr-ship-and-review.md`, https://github.com/the-hcma/repository-helpers/blob/main/scripts/lib/repo-practices-agents/rules/pr-ship-and-review.md): it still carries the `gt` snippets until rendered. Keep a thin `.cursor/rules/pr-ship-and-review.mdc` shim.
3. Delete root `GRAPHITE.md` when switching to `gh-stack`.
4. Keep `.agents/rules/stacking-tool.md` (+ Cursor shim) in sync with the consumer template.
5. Re-run `scripts/github-repo-lint --repo OWNER/NAME --suggest --strict-onboarding` and fix stacking-docs consistency findings.

## Unchanged regardless of marker

Agent review, CI wait, and reply-before-resolve still follow `.agents/rules/pr-ship-and-review.md` and the canonical ship-and-review skill in repository-helpers.
