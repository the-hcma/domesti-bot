---
description: CodeRabbit CLI skills are installed user-level with coderabbit skills; the review run caps and the no-credits rule stay in force
alwaysApply: true
---

# CodeRabbit CLI skills

The CodeRabbit CLI ships agent skills (`code-review`, `autofix`, `coderabbit-config`). Install and update them at user level with `coderabbit skills`, never as copied skill bodies committed to this repository. Under an agent, preview with `coderabbit skills --agent` and run the exact `--confirm <plan-hash>` command only after the user approves the plan. Never pass `--use-credits`, and keep using the review run caps instead of bypassing them with a skill invocation.

Read the full policy before using any CodeRabbit skill: the canonical rule in repository-helpers, linked below, carries the review run caps, the reply-before-resolve triage for `autofix` and the user-approval requirement for `coderabbit-config` saves. The paths it names (`scripts/dev/coderabbit-cli-review`, `scripts/dev/pre-pr-checks`, `scripts/dev/post-pr-submission-checks`, `.agents/skills/ship-and-review/SKILL.md`) live in the repository-helpers checkout (`${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}`), not in this repository.

<!-- coderabbit-skills-canonical: https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/coderabbit-skills.md -->
https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/coderabbit-skills.md
