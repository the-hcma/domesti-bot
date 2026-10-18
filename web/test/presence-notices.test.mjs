import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { presenceForgetConfirmMessage, unseenPresenceNotices } from "../.test-build/presence-notices.mjs";

const notice = (id) => ({
  device_id: "5c:e9:31:60:c5:bd",
  family_id: "kasa",
  host: "192.168.86.167",
  kind: "disappeared",
  label: "Master bedroom fan",
  message: "stopped responding",
  notice_id: id,
  recorded_at: 1000,
});

describe("unseenPresenceNotices", () => {
  it("returns only the notices whose ids have not been seen, in order", () => {
    const notices = [notice("a"), notice("b"), notice("c")];
    assert.deepEqual(
      unseenPresenceNotices(notices, new Set(["b"])).map((n) => n.notice_id),
      ["a", "c"],
    );
  });

  it("returns nothing when every notice was already seen", () => {
    assert.deepEqual(unseenPresenceNotices([notice("a")], new Set(["a"])), []);
  });
});

describe("presenceForgetConfirmMessage", () => {
  it("names the device and says its name and settings are kept", () => {
    const message = presenceForgetConfirmMessage("Master bedroom fan");
    assert.match(message, /Master bedroom fan/);
    assert.match(message, /name and settings are kept/);
  });
});
