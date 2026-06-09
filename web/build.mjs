// Bundle the browser TypeScript into app/api/static/dist/.
//
// The Python FastAPI server mounts that directory at `/static/`, so the
// resulting URL for the entrypoint is `/static/dist/main.js`.
//
// Usage:
//   node build.mjs           # one-shot build
//   node build.mjs --watch   # rebuild on change (dev only)
//
// The build is intentionally tiny and dependency-light: just esbuild + tsc.
// `pnpm run typecheck` runs `tsc --noEmit`; this script only emits.

import { build, context } from "esbuild";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..");
const outdir = path.join(repoRoot, "app", "api", "static", "dist");

const watch = process.argv.includes("--watch");

/** @type {import("esbuild").BuildOptions} */
const options = {
  entryPoints: [path.join(here, "src", "main.ts")],
  outdir,
  outExtension: { ".js": ".js" },
  bundle: true,
  format: "esm",
  target: ["es2022"],
  platform: "browser",
  sourcemap: true,
  minify: !watch,
  logLevel: "info",
  legalComments: "none",
  // Stable filename; index.html refers to /static/dist/main.js by name.
  // Hashing/cache-busting is added in a later PR once we wire up real assets.
  entryNames: "[name]",
};

if (watch) {
  const ctx = await context(options);
  await ctx.watch();
  console.log(`[esbuild] watching → ${path.relative(repoRoot, outdir)}`);
} else {
  await build(options);
  console.log(`[esbuild] built → ${path.relative(repoRoot, outdir)}`);
}
