# `web/` — domesti-bot browser bundle

TypeScript source for the browser-side tile dashboard served by the
domesti-bot FastAPI server. Compiled output is written to
`../app/api/static/dist/` and served at `/static/dist/` by the Python server.

## Conventions

- **Toolchain:** `pnpm` + `esbuild` + `typescript`. `pnpm` is bootstrapped via
  Node's built-in `corepack` (no extra global install needed). Node ≥ 20.
- **No bundler magic:** one `esbuild` call. No Vite, no Webpack, no Rollup.
- **No frameworks** in PR1. Vanilla TypeScript + DOM. Frameworks (if ever
  needed) are an explicit, documented decision per stack — not an organic
  drift.
- **Strict TS:** `strict`, `noUncheckedIndexedAccess`,
  `exactOptionalPropertyTypes`, `verbatimModuleSyntax`. Existing files do not
  silence these.
- **Build output is not committed.** `app/api/static/dist/` is gitignored.
  CI rebuilds; production rebuilds via `scripts/on-deploy`. Runtime requires
  Node only at *deploy time*, not at *serve time*.

## One-time setup

```bash
corepack enable pnpm                   # provided by Node ≥ 16.10
cd web && pnpm install --frozen-lockfile
```

## Day-to-day

```bash
pnpm run typecheck    # tsc --noEmit (no output)
pnpm run build        # esbuild → ../app/api/static/dist/main.js
pnpm run watch        # rebuild on change (use during dev)
pnpm run check        # typecheck + build (mirrors CI)
```

After a build, the FastAPI server picks up the new `dist/main.js` on the next
page load — no Python restart required, because `app/api/static/` is served as
plain static files.

## PWA

The landing page is a Progressive Web App: `app/api/static/manifest.webmanifest`,
`app/api/static/sw.js` (also at `/sw.js`), and launcher icons under
`app/api/static/icons/`. The bundle registers the service worker on load. When
HTML, inline CSS, or this bundle changes in a release, bump the `VERSION`
string in `sw.js` so installed clients refresh their cache. See the root
`README.md` *Progressive Web App* section for install requirements (HTTPS or
loopback).
