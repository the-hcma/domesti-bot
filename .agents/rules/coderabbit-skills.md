---
description: CodeRabbit CLI skills are installed user-level with coderabbit skills; the review run caps and the no-credits rule stay in force
alwaysApply: true
---

# CodeRabbit CLI skills

The CodeRabbit CLI ships agent skills (`code-review`, `autofix`, `coderabbit-config`). Install and update them at user level with `coderabbit skills`, never as copied skill bodies committed to this repository. Under an agent, preview with `coderabbit skills --agent` and run the exact `--confirm <plan-hash>` command only after the user approves the plan. Never pass `--use-credits`, and keep using the review run caps instead of bypassing them with a skill invocation.

Full policy (what each skill may do here, the pre-push review caps, the reply-before-resolve triage for `autofix`) — the canonical rule in repository-helpers:

<!-- coderabbit-skills-canonical: https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/coderabbit-skills.md -->
https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/coderabbit-skills.md
