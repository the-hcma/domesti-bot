import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formatAnyTimingAlternatives } from "../.test-build/rule-summary.mjs";

const window = (start, end) => ({ type: "local_time_window", start_hhmm: start, end_hhmm: end });

describe("formatAnyTimingAlternatives", () => {
  it("joins several time windows into one line", () => {
    const line = formatAnyTimingAlternatives({
      type: "any",
      conditions: [window("06:30", "09:30"), window("19:00", "22:00")],
    });
    assert.equal(line, "Between 6:30 AM and 9:30 AM, or between 7:00 PM and 10:00 PM");
  });

  it("gives up when a child is not a timing condition", () => {
    const line = formatAnyTimingAlternatives({
      type: "any",
      conditions: [window("06:30", "09:30"), { type: "users_inside_geofence", geofence_id: "house", user_ids: ["a"] }],
    });
    assert.equal(line, null);
  });

  it("gives up on an empty group", () => {
    assert.equal(formatAnyTimingAlternatives({ type: "any", conditions: [] }), null);
  });
});
