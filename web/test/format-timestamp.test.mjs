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

describe("formatLocalTimestamp", () => {
  it("renders local time with a zone label and the same instant in UTC in parentheses", () => {
    const stamp = withTimeZone("America/New_York", () => formatLocalTimestamp("2026-11-26T03:58:45Z"));
    // The local part is the previous evening in New York; the zone label's spelling is locale-dependent.
    assert.match(stamp, /11\/25\/2026/);
    assert.match(stamp, /(EST|GMT-5|UTC-5)/);
    assert.ok(stamp.endsWith("(2026-11-26 03:58:45 UTC)"), stamp);
  });

  it("shows the instant once when the local zone is UTC", () => {
    const stamp = withTimeZone("UTC", () => formatLocalTimestamp("2026-11-26T03:58:45Z"));
    assert.ok(stamp.endsWith(" UTC"), stamp);
    assert.ok(!stamp.includes("("), stamp);
  });

  it("formats an epoch the same way, with UTC in parentheses", () => {
    const stamp = withTimeZone("America/New_York", () => formatLocalTimestampFromEpoch(1_700_000_000));
    assert.ok(stamp.endsWith("(2023-11-14 22:13:20 UTC)"), stamp);
  });

  it("returns a non-finite epoch as text rather than throwing", () => {
    assert.equal(formatLocalTimestampFromEpoch(Number.NaN), "NaN");
  });

  it("returns unparseable input unchanged", () => {
    assert.equal(formatLocalTimestamp("not-a-date"), "not-a-date");
  });
});
