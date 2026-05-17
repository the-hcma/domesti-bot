# Graphite workflow

Reference for Graphite (`gt`) stacked PRs and the **Graphite merge queue** on `main`.

> **Session start:** run `~/work/ai/repository-helpers/scripts/dev/start-development --refresh` then `start-development` (see `docs/AGENTS.md`).

## Quick reference

| I want to… | Command |
| --- | --- |
| Sync at session start | `gt sync --force` |
| Create branch + commit (staged) | `gt create --all --message "feat: …"` |
| Amend current branch | `gt modify --all --message "…"` or `gt modify --no-edit` |
| Submit (ready for review) | `gt submit --no-interactive --publish` |
| View stack | `gt log short` |
| Enqueue after approval | `gh pr edit <pr> --add-label merge-it` |

Always pass **`--publish`** on `gt submit` (otherwise PRs stay draft).

---

## Merge queue

This repo uses the [Graphite merge queue](https://graphite.dev/docs/graphite-merge-queue) on **`main`**. Do **not** merge via the GitHub UI or `gh pr merge`.

### Enqueue a PR

After CI is green and the user approves:

```bash
gh pr edit <pr-number> --add-label merge-it
```

The repo label **`merge-it`** is wired in Graphite merge-queue settings (“enqueue via label”). Dependabot PRs get the label automatically (`.github/workflows/dependabot-auto-merge.yml`).

### Enable / configure in Graphite

1. [Graphite merge queue settings](https://app.graphite.com/settings/merge-queue)
2. **Add merge queue** → select **`the-hcma/domesti-bot`**
3. Set merge strategy (squash recommended for stacked work), timeout, and confirm the enqueue label is **`merge-it`**

Requires Graphite Team/Enterprise and the **Graphite GitHub App** on the org.

### GitHub ruleset (`protect-main`)

`main` is protected by ruleset **`protect-main`** (not classic branch protection). For merge-queue **optimizations**, Graphite must bypass required PR rules:

| Setting | Value |
| --- | --- |
| Ruleset | `protect-main` → **Bypass list** |
| Actor | **Graphite App** (`graphite-app`) |
| Mode | **Always allow** |

This matches GitHub’s “Allow specified actors to bypass required pull requests” for rulesets. Graphite cannot validate ruleset config in-app; if MQ setup fails, confirm bypass in [Rules → Rulesets](https://github.com/the-hcma/domesti-bot/rules).

**Graphite App IDs** (org `the-hcma`): GitHub App `app_id` **158384**, installation **108210318**. Ruleset bypass uses `actor_type: Integration`, `actor_id: 158384`.

To re-apply via API (repo admin):

```bash
gh api -X PUT repos/the-hcma/domesti-bot/rulesets/16365386 --input - <<'JSON'
{
  "name": "protect-main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {"include": ["refs/heads/main"], "exclude": []}
  },
  "bypass_actors": [
    {
      "actor_id": 158384,
      "actor_type": "Integration",
      "bypass_mode": "always"
    }
  ],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {
      "type": "pull_request",
      "parameters": {
        "allowed_merge_methods": ["merge", "squash", "rebase"],
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": false,
        "required_reviewers": []
      }
    }
  ]
}
JSON
```

**Optional (recommended):** restrict direct pushes to `main` so only the merge queue lands commits—configure in GitHub rules / branch settings per [Graphite’s merge-queue setup](https://graphite.dev/docs/set-up-merge-queue) (push allow list: `graphite-app`).

### CI and merge queue

- CI skips `graphite-base/**` branches and re-runs on open PRs (see `.github/workflows/ci.yml`).
- `merged-pr-closer.yml` closes child PRs left open after a stack merges via the queue.

---

## Stacked PR workflow

1. Work in a **dedicated worktree** (`start-development --worktree <name> --no-interactive`).
2. `gt create --all --message "feat: …"` on a feature branch; never commit on `main`.
3. Pre-PR gates in `docs/AGENTS.md` (pyright, pytest, shellcheck, …).
4. `gt submit --no-interactive --publish`.
5. Wait for CI; get user approval; add **`merge-it`**.

---

## Troubleshooting

| Problem | What to do |
| --- | --- |
| MQ: “Allow specified actors to bypass…” | Add **Graphite App** to ruleset **Bypass list** (above). |
| Stale-base-ref in queue | `gt sync --force` on the stack, restack, push. |
| Untracked branch | `gt track --parent main` |
| Draft PR | Re-submit with `--publish` |
