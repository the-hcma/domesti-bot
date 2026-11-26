import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formatLocalTimestamp } from "../.test-build/format-timestamp.mjs";

describe("formatLocalTimestamp", () => {
  it("renders local time with its zone and the same instant in UTC in parentheses", () => {
    const previous = process.env.TZ;
    process.env.TZ = "America/New_York";
    try {
      const stamp = formatLocalTimestamp("2026-11-26T03:58:45Z");
      assert.match(stamp, /EST/);
      assert.ok(stamp.endsWith("(2026-11-26 03:58:45 UTC)"), stamp);
    } finally {
      if (previous === undefined) {
        delete process.env.TZ;
      } else {
        process.env.TZ = previous;
      }
    }
  });

  it("never returns a bare UTC ISO string for a valid timestamp", () => {
    assert.ok(!formatLocalTimestamp("2026-11-26T03:58:45Z").includes("T03:58:45Z"));
  });

  it("returns unparseable input unchanged", () => {
    assert.equal(formatLocalTimestamp("not-a-date"), "not-a-date");
  });
});
