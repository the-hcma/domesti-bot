---
description: Format agent-authored GitHub issue/PR bodies and comments so they render correctly (blank lines + no hand-wrapped paragraphs)
alwaysApply: true
---

# GitHub content formatting (agent-authored)

Agent-authored issue bodies, PR descriptions, and PR/review comments must render
correctly on GitHub: separate paragraphs and list items with a blank line, and
write one physical line per paragraph — GitHub's issue/PR/comment renderer treats
a lone `\n` as a **visible hard break**, unlike the file/blob renderer's
soft-break-as-space.

Write multi-paragraph or multi-line-list bodies to a temp file and post with
`--body-file <path>`, never an inline `--body "..."` string with embedded `\n`
escapes. Lint before posting:

```bash
"${REPOSITORY_HELPERS_DIR:-$HOME/work/ai/repository-helpers}/scripts/lint-github-markdown" <path>
```

Full authoring rules, the pre-flight linter, and `scripts/gh-issue` — the canonical
rule in repository-helpers:

<!-- github-content-formatting-canonical: https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/github-content-formatting.md -->
https://github.com/the-hcma/repository-helpers/blob/main/.agents/rules/github-content-formatting.md
