---
description: Load every agent rule under .agents/rules/ (the canonical rule bodies)
applyTo: '**'
---

# Agent rules

This repository's agent rules live under [`.agents/rules/`](../../.agents/rules/), one Markdown file per rule. [`AGENTS.md`](../../AGENTS.md) and those rule files together are the contract; neither alone is complete.

At the start of every session, before acting from assumed conventions, read every rule under `.agents/rules/*.md` whose front matter has `alwaysApply: true`, plus any rule whose `globs` match files you will touch. Follow them for the whole session.

Read them again after any conversation compaction or summary (`/compact`, a session summary, or a resumed session). A summary does not carry the rule bodies, so "I already read the rules" does not survive it.

This file is a thin pointer so tools that auto-load `.github/instructions/*.instructions.md` (GitHub Copilot) always receive it. Do not copy rule bodies here; edit the rule under `.agents/rules/` instead.

Org template: repository-helpers#681.
