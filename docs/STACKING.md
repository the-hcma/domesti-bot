# Stacking and merge queue

Reference for **`gh-stack`** stacked PRs and **GitHub’s native merge queue** on `main`.

Stacking backend is selected by `.github/stacking-tool` (this repo: `gh-stack`). Canonical
skills live in [repository-helpers](https://github.com/the-hcma/repository-helpers):

- [gh-stack skill](https://github.com/the-hcma/repository-helpers/blob/main/.cursor/skills/gh-stack/SKILL.md)
- [ship-and-review skill](https://github.com/the-hcma/repository-helpers/blob/main/.cursor/skills/ship-and-review/SKILL.md)

> **Session start:** run `~/work/ai/repository-helpers/scripts/dev/start-development --refresh`
> then `start-development` (see `docs/AGENTS.md`).

## Quick reference

| I want to… | Command |
| --- | --- |
| Sync at session start | `start-development --refresh` (marker-aware) |
| Create first stack branch | `gh stack init <stack>/<topic>` |
| Add a dependent layer | `gh stack add <stack>/<next-topic>` |
| Commit | `git add … && git commit -m 'feat: …'` |
| Submit / update PRs | `~/work/ai/repository-helpers/scripts/dev/submit-stack` |
| View stack (agents) | `gh stack view --json` |
| Enqueue after approval | `gh pr merge <pr> --auto --squash` |

Install the extension once: `gh extension install github/gh-stack`. Prefer
`scripts/dev/submit-stack` from repository-helpers over bare `gh stack submit`
(it runs local gates, then `gh stack submit --auto --open --remote origin`).

---

## Merge queue

This repo uses **GitHub’s native merge queue** on **`main`** (not the Graphite merge queue).

### Enqueue a PR

After CI is green and the user approves:

```bash
gh pr merge <pr-number> --auto --squash
```

Or use **Enable auto-merge** / **Merge when ready** in the GitHub UI. Squash is required
(Release Please — see [`docs/RELEASING.md`](RELEASING.md)).

Do **not** add the `merge-it` label — that enqueues Graphite MQ on other org repos.

Dependabot PRs get auto-merge enabled automatically
(`.github/workflows/dependabot-auto-merge.yml`).

### Disable Graphite merge queue

Turn this repo **off** in [Graphite merge queue settings](https://app.graphite.com/settings/merge-queue)
so Graphite and GitHub do not both try to land PRs.

### GitHub ruleset (`protect-main`)

`main` is protected by ruleset **`protect-main`** plus classic branch protection.

| Setting | Value |
| --- | --- |
| Ruleset | `protect-main` on `refs/heads/main` |
| Bypass list | **empty** (no Graphite App bypass) |
| Extra rule | `merge_queue` with `merge_method: SQUASH` |
| Repo setting | `allow_auto_merge: true` |
| Classic push restrictions | **none** (GitHub MQ lands merges) |

To re-apply the ruleset via API (repo admin):

```bash
gh api -X PUT repos/the-hcma/domesti-bot/rulesets/16365386 --input - <<'JSON'
{
  "name": "protect-main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {"include": ["refs/heads/main"], "exclude": []}
  },
  "bypass_actors": [],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {
      "type": "pull_request",
      "parameters": {
        "allowed_merge_methods": ["squash"],
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": false,
        "required_reviewers": []
      }
    },
    {
      "type": "merge_queue",
      "parameters": {
        "check_response_timeout_minutes": 60,
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 5,
        "max_entries_to_merge": 5,
        "merge_method": "SQUASH",
        "min_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 0
      }
    }
  ]
}
JSON
```

Repair classic protection / related wiring with
`~/work/ai/repository-helpers/scripts/github-repo-lint --repo the-hcma/domesti-bot --apply-fix`
from a clone once the merge queue is enabled.

### CI and merge queue

- CI **runs** on `merge_group` (required status checks for GitHub’s merge queue) and
  **ignores pushes** to `gh-readonly-queue/**` so those temporary branches do not
  double-trigger via the `push` event.
- Guard still skips legacy Graphite staging (`gtmq_merge_*`) only — do **not** skip
  `merge_group` / `gh-readonly-queue/*` in Guard or required checks never pass in queue.
- `merged-pr-closer.yml` still closes child PRs left open after a stack lands.

---

## Stacked PR workflow

1. Work in a **dedicated worktree** (`start-development --worktree <name> --no-interactive`).
2. `gh stack init <name>/<topic>` (then `git commit`); never commit on `main`.
3. Pre-PR gates in `docs/AGENTS.md` (pyright, pytest, shellcheck, …).
4. `~/work/ai/repository-helpers/scripts/dev/submit-stack`.
5. Wait for CI; get user approval; `gh pr merge <pr> --auto --squash`.

---

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Two queues fighting | Disable this repo in Graphite MQ settings. |
| Auto-merge unavailable | Ensure `allow_auto_merge` is on and `protect-main` has `merge_queue`. |
| Stale stack after land | `gh stack sync` / `gh stack rebase` from the worktree. |
| Interactive `gh stack` hang | Always pass non-interactive flags (`view --json`, `submit --auto`, named `init`/`add`). |
