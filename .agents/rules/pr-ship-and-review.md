---
description: Ship a PR end-to-end — local gates, submit, agent review loop, operator email
alwaysApply: true
---

# PR ship, agent review, and operator email

When the user asks to **ship**, **submit**, **open a PR**, or **follow the flow**, run this sequence in a **stack worktree** (never the primary clone). Read `.github/stacking-tool` and `.cursor/rules/stacking-tool.mdc` before creating branches or submitting.

Helper scripts live in **repository-helpers** (canonical agent review loop):

```bash
rh="${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}"
"${rh}/scripts/wait-for-agent-review" …
"${rh}/scripts/trigger-agent-review" …
```

Configure `~/.config/agent-review.env` from `${rh}/etc/agent-review.env.example` (`AGENT_REVIEW_REPORT_TO`, SMTP).

<!-- pr-ship-canonical-skill: https://github.com/the-hcma/repository-helpers/blob/main/.agents/skills/ship-and-review/SKILL.md -->
<!-- pr-ship-canonical-rule: https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/pr-ship-and-review.md -->

**Canonical playbook (read first):** This file is a consumer summary. Exit codes, early-complete semantics, quota chains, and triage details are maintained in repository-helpers and may change between releases.

Agents **must** read the canonical Skill before running the agent review loop:

- **GitHub (`main`):** https://github.com/the-hcma/repository-helpers/blob/main/.agents/skills/ship-and-review/SKILL.md
- **Local clone (when `${rh}` is synced to `main`):** `${rh}/.agents/skills/ship-and-review/SKILL.md`

When this summary and the canonical Skill disagree, follow the canonical Skill.

## 1. Local quality gates (all must pass before submit)

Run this repository's quality gates from the stack worktree (tests, linters, etc.).

## 2. Commit and submit

Only after §1 quality gates pass. **Read** `.github/stacking-tool` (`graphite` or `gh-stack`) and follow `.cursor/rules/stacking-tool.mdc` — do not mix backends on the same stack.

```bash
# When marker is graphite (apply-fix comments the inactive backend):
# gt create <stack>/<topic> -m 'feat: …'   # or gt modify -m '…'
# gt submit --publish --no-interactive

# When marker is gh-stack:
gh stack init <stack>/<topic>   # or gh stack add <branch> for a higher layer
#   (the branch argument keeps it non-interactive)
git add -A && git commit -m 'feat: …'
gh stack submit --auto --open --remote origin
```

Wait for CI:

```bash
"${rh}/scripts/dev/post-pr-submission-checks" --pr <n>
```

If stderr shows `NOTE: GITHUB_RATE_LIMIT_*`, the helpers are waiting on GitHub API quota reset — let them finish (do not treat as a hard local failure mid-wait).

`--auto` / publish-generated PR titles are derived from the branch name (or a single commit subject), **not** always from Conventional Commits. Before the review loop or merge, verify/set the PR title to a Conventional Commits header whenever the repo's squash-merge config makes the PR title the release-please signal (`squash_merge_commit_title=PR_TITLE`, `squash_merge_commit_message=BLANK`):

```bash
"${rh}/scripts/ensure-pr-conventional-title" --pr <n>
# or: "${rh}/scripts/gh-api" pr edit <n> --title 'feat: …'
```

`post-pr-submission-checks` and `wait-for-agent-review complete` run this check automatically (auto-derive from commits when possible; fail when neither the title nor commits are Conventional Commits).

Patch title/body if stale. For multi-paragraph bodies use `--body-file` (see `${rh}/.agents/rules/github-content-formatting.md`); lint first with `"${rh}/scripts/lint-github-markdown" <path>`. Issues: `"${rh}/scripts/gh-issue" create|edit` (lint before API). Do not hand-wrap paragraphs across short lines.

```bash
"${rh}/scripts/gh-api" pr edit <n> --title 'feat: …' --body-file /tmp/pr-body.md
"${rh}/scripts/gh-issue" create --title '…' --body-file /tmp/issue.md
```

### Closing the issue

Say in the PR description whether the merge should close the issue. Default, unless the repository's own `AGENTS.md` states a different convention (that wins):

1. A PR that completes an issue puts `Closes #N.` in its description as its own sentence, so the merge closes the issue. For an issue in another repository, qualify it: `Closes OWNER/REPOSITORY#N.`, because a bare `#N` means an issue in the PR's own repository.
2. A PR that is only one step of a larger issue says `Part of #N.` and does not close it (for an issue in another repository write `Part of OWNER/REPOSITORY#N.`). The last PR of the series closes the issue, and it must target the default branch when it merges: GitHub ignores a closing keyword on a PR into any other branch (for example a lower layer of a stack), so close the issue explicitly after the work lands.
3. A PR with a closing keyword is merged only after the user agrees the issue is done, because the merge closes it. The merge confirmation this workflow already requires covers that.

Avoid applying a "no closing keywords" convention to every PR regardless of whether it finishes the issue: finished issues then stay open and need a manual close pass. Spell the keyword the way the "Unlinked closing keywords" failure mode in `${rh}/.agents/rules/github-content-formatting.md` requires.

## 3. Agent review loop

> **Reply before resolve (required):** For every valid agent review thread (Copilot, Bugbot, CodeRabbit, …), post an **on-thread human reply** as the authenticated operator **before** resolving. Exit code **3** means threads **lack a human reply** — resolving without replying is non-compliant.

**Prerequisites:** `gh auth` must be set up for the operator running the loop. Copy `${rh}/etc/agent-review.env.example` to `~/.config/agent-review.env` with `AGENT_REVIEW_REPORT_TO` and SMTP before `complete`. `complete_ready: true` is reported by `"${rh}/scripts/wait-for-agent-review" check --pr <n>` (or `status`) JSON when CI is green and agent review threads are clear.

Prefer the built-in loop:

```bash
"${rh}/scripts/wait-for-agent-review" loop --pr <n>
```

Run the loop directly as its own tracked background command (for example `run_in_background` in Claude Code), never through a launcher that detaches it, so its own exit notifies the caller; a notification naming a launcher or wrapper is not a loop result (confirm with the process list and the log tail), a wrapper `echo` exit code is not the loop's (read the result it recorded, for example a `loop exit=` line in its log; no first-party script writes such a line), and waiters longer than the harness's background cap must be chunked and re-armed with a clock check at each chunk end. The loop exits **3** when feedback needs a reply or resolve and nothing else pushes that to anyone, so triage immediately on exit 3 (read `check` and `list-feedback`, fix or decline, then reply and resolve by feedback kind — `reply-thread` for review threads, `reply-comment` for issue comments and review bodies — and push). Full detail: canonical Skill §3.

**Early complete:** when all threads are addressed, CI is green, there is **no** pending requested Copilot/Bugbot review, and CodeRabbit’s workflow is **not** running, the loop finishes — no mandatory idle dwell. **12h PR cap** (`AGENT_REVIEW_PR_TIMEOUT`) when review cycles never converge (exit **6** give-up). Per-agent quota caches skip exhausted agents (CodeRabbit, Copilot, Bugbot).

**CodeRabbit is on_push:** a new push starts CodeRabbit — never post `@coderabbitai review`. If quota-limited, wait the cooldown with feedback polls (issue #369; default poll **60s**) + grace (default **60s**); only then, if still no real review on head, the loop may post a one-shot `@coderabbitai full review`. When CodeRabbit says wait, do not re-ask until `retry_after`. Rate-limit stubs are not reviews. Mid-cooldown pending feedback wakes the loop (exit **3**).

When `loop` exits **3**, triage each unaddressed agent thread:

1. Fix in the worktree when actionable (or skip with a brief on-thread reason).
2. **Reply on-thread** as the authenticated human:
   ```bash
   "${rh}/scripts/wait-for-agent-review" reply-thread --thread-id <PRRT_…> --body 'Fixed in abc1234: …'
   ```
3. **Resolve only after step 2:**
   ```bash
   "${rh}/scripts/wait-for-agent-review" resolve-thread --thread-id <PRRT_…>
   ```
   Or batch: `"${rh}/scripts/wait-for-agent-review" resolve-addressed --pr <n>` (requires an existing human reply per thread).

**Never** resolve via raw GraphQL/API without an on-thread human reply first.

Then push fixes, re-run `"${rh}/scripts/dev/post-pr-submission-checks" --pr <n>`, and start the next loop iteration.

When `check` reports `complete_ready: true`:

```bash
"${rh}/scripts/wait-for-agent-review" complete --pr <n>
```

`complete_ready` requires an agent sign-off on the current head (not a bare CodeRabbit commit status / rate-limit stub). This emails `AGENT_REVIEW_REPORT_TO`. It does **not** run `gh pr review --approve` (self-approve is skipped; merge stays an explicit operator step).

Never add the `merge-it` label: it is ignored and the org no longer uses Graphite merge queue. Merge only after the user explicitly confirms, and only with `scripts/dev/merge-stack --pr <n>` (from the PR's worktree; in a consumer repo `${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}/scripts/dev/merge-stack --pr <n>`): it picks the right command for the repository's merge setup (`gh stack merge` for a stacked PR, the merge queue or auto-merge otherwise) and confirms the enqueue. For a PR in a gh stack, pass the top PR's number and run it from that stack's worktree: it refuses a lower layer, and it lands the top PR and every open PR below it, so name those PRs and get confirmation for the whole stack first. Do not call `gh pr merge` or `gh stack merge` by hand, do not add the `merge-it` label, and do not pipe the command through `tail` or `head`: read its exit status and output.
