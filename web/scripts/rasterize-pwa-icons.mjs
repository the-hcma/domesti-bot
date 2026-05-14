// Rasterize ``app-icon.svg`` into PNGs for the web app manifest and Apple touch.
// Run from ``web/`` via ``pnpm run build`` (after ``pnpm install``).

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import sharp from "sharp";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");
const svgPath = path.join(repoRoot, "app", "api", "static", "icons", "app-icon.svg");
const outDir = path.join(repoRoot, "app", "api", "static", "icons");

const bg = { r: 246, g: 247, b: 249, alpha: 1 };

async function main() {
  const input = fs.readFileSync(svgPath);
  for (const size of [192, 512]) {
    const outPath = path.join(outDir, `icon-${size}.png`);
    await sharp(input)
      .resize(size, size, { fit: "contain", background: bg })
      .png()
      .toFile(outPath);
    console.log(`[pwa-icons] wrote ${path.relative(repoRoot, outPath)}`);
  }
}

void main();
