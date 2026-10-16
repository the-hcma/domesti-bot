import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formatAnyTimingAlternatives, formatTogetherLabel } from "../.test-build/rule-summary.mjs";

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

describe("formatTogetherLabel", () => {
  const context = {
    deviceLabelByKey: new Map(),
    geofenceLabelById: new Map(),
    userDisplayNameById: new Map([
      ["user-a", "User A"],
      ["user-b", "User B"],
    ]),
  };
  const condition = (extra = {}) => ({ type: "users_together", user_ids: ["user-a", "user-b"], ...extra });

  it("names the users and the default confidence", () => {
    assert.equal(formatTogetherLabel(condition(), context), "User A and User B travelling together (confidence at least 80%)");
  });

  it("uses the configured confidence", () => {
    assert.equal(
      formatTogetherLabel(condition({ min_confidence: 0.9 }), context),
      "User A and User B travelling together (confidence at least 90%)",
    );
  });

  it("describes the apart form with the complementary ceiling", () => {
    assert.equal(
      formatTogetherLabel(condition({ expected: false }), context),
      "User A and User B not travelling together (confidence at most 20%)",
    );
  });
});
