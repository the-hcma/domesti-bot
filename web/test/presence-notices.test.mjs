import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  presenceForgetConfirmMessage,
  presenceToastFor,
  unseenPresenceNotices,
  withoutPresenceNotices,
} from "../.test-build/presence-notices.mjs";

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

describe("presenceToastFor", () => {
  const returned = (id, label) => ({ ...notice(id), kind: "reappeared", label, message: `${label} is responding again.` });
  const missing = (id, label) => ({ ...notice(id), label, message: `${label} stopped responding.` });

  it("returns nothing when no notice is new", () => {
    assert.equal(presenceToastFor([]), null);
  });

  it("shows the notice text for a single missing device as an error", () => {
    assert.deepEqual(presenceToastFor([missing("a", "Fan")]), { message: "Fan stopped responding.", variant: "error" });
  });

  it("summarizes several missing devices in one error toast", () => {
    const toast = presenceToastFor([missing("a", "Fan"), missing("b", "Lamp")]);
    assert.equal(toast.variant, "error");
    assert.equal(toast.message, "2 devices stopped responding: Fan, Lamp.");
  });

  it("lets missing devices win over returned ones", () => {
    assert.equal(presenceToastFor([returned("a", "Fan"), missing("b", "Lamp")]).variant, "error");
  });

  it("shows returned devices as a success toast, summarized when several", () => {
    assert.deepEqual(presenceToastFor([returned("a", "Fan")]), { message: "Fan is responding again.", variant: "success" });
    assert.equal(
      presenceToastFor([returned("a", "Fan"), returned("b", "Lamp")]).message,
      "2 devices are responding again: Fan, Lamp.",
    );
  });
});

describe("withoutPresenceNotices", () => {
  it("drops notices whose ids are in any of the hidden sets, keeping order", () => {
    const notices = [notice("a"), notice("b"), notice("c")];
    assert.deepEqual(
      withoutPresenceNotices(notices, new Set(["a"]), new Set(["c"])).map((n) => n.notice_id),
      ["b"],
    );
  });

  it("keeps a handled notice hidden when a stale poll brings it back", () => {
    const handled = new Set(["a"]);
    const stalePoll = [notice("a"), notice("b")];
    assert.deepEqual(
      withoutPresenceNotices(stalePoll, new Set(), handled).map((n) => n.notice_id),
      ["b"],
    );
  });

  it("returns every notice when nothing is hidden", () => {
    assert.equal(withoutPresenceNotices([notice("a")], new Set()).length, 1);
  });
});
