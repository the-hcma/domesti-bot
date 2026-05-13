// Browser entrypoint for the domesti-bot landing page.
//
// PR1 of the web-ui-tiles stack: this only proves the TypeScript toolchain is
// wired end-to-end. It looks up `<span id="bundle-status">` in the static
// landing page and flips its text + class once the module evaluates, so a
// human or a test can confirm the bundle was fetched and executed.
//
// The actual tile grid (Kasa first, then Tailwind) lands in later PRs.

const STATUS_ID = "bundle-status";

function markBundleReady(): void {
  const el = document.getElementById(STATUS_ID);
  if (!el) {
    console.warn(`[domesti-bot] expected #${STATUS_ID} in landing page`);
    return;
  }
  el.textContent = "loaded";
  el.dataset["state"] = "ready";
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", markBundleReady, { once: true });
} else {
  markBundleReady();
}
