import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formatLocalTimestamp, formatLocalTimestampFromEpoch } from "../.test-build/format-timestamp.mjs";

function withTimeZone(zone, fn) {
  const previous = process.env.TZ;
  process.env.TZ = zone;
  try {
    return fn();
  } finally {
    if (previous === undefined) {
      delete process.env.TZ;
    } else {
      process.env.TZ = previous;
    }
  }
}

// The locale is pinned in every assertion about text so a host with another default locale
// cannot change the result; production code leaves it unset and uses the viewer's locale.
describe("formatLocalTimestamp", () => {
  it("renders local time with a zone label and the same instant in UTC in parentheses", () => {
    const stamp = withTimeZone("America/New_York", () => formatLocalTimestamp("2026-11-26T03:58:45Z", "en-US"));
    assert.equal(stamp, "11/25/2026, 10:58:45 PM EST (2026-11-26 03:58:45 UTC)");
  });

  it("keeps the UTC suffix and the previous local date in another locale", () => {
    const stamp = withTimeZone("America/New_York", () => formatLocalTimestamp("2026-11-26T03:58:45Z", "en-GB"));
    assert.ok(stamp.startsWith("25/11/2026"), stamp);
    assert.ok(stamp.endsWith("(2026-11-26 03:58:45 UTC)"), stamp);
  });

  it("shows the instant once when the local zone is UTC, whatever the locale puts first", () => {
    for (const locale of ["en-US", "zh-CN", "de-DE"]) {
      const stamp = withTimeZone("UTC", () => formatLocalTimestamp("2026-11-26T03:58:45Z", locale));
      assert.ok(stamp.includes("UTC"), `${locale}: ${stamp}`);
      assert.ok(!stamp.includes("("), `${locale}: ${stamp}`);
    }
  });

  it("formats an epoch the same way, with UTC in parentheses", () => {
    const stamp = withTimeZone("America/New_York", () => formatLocalTimestampFromEpoch(1_700_000_000, "en-US"));
    assert.equal(stamp, "11/14/2023, 5:13:20 PM EST (2023-11-14 22:13:20 UTC)");
  });

  it("returns a non-finite epoch as text rather than throwing", () => {
    assert.equal(formatLocalTimestampFromEpoch(Number.NaN), "NaN");
  });

  it("returns unparseable input unchanged", () => {
    assert.equal(formatLocalTimestamp("not-a-date"), "not-a-date");
  });
});
