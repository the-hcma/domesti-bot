import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { WRITE_ONLY_SAVED_PLACEHOLDER, applyWriteOnlySecretState } from "../.test-build/settings-secret-field.mjs";

const fakeInput = (value = "typed") => ({ value, required: false, placeholder: "" });

describe("applyWriteOnlySecretState", () => {
  it("clears the field, shows the saved placeholder and drops `required` once a value is stored", () => {
    const input = fakeInput();
    applyWriteOnlySecretState(input, { configured: true, emptyPlaceholder: "Account password" });
    assert.equal(input.value, "");
    assert.equal(input.required, false);
    assert.equal(input.placeholder, WRITE_ONLY_SAVED_PLACEHOLDER);
  });

  it("requires a value and shows the empty placeholder when nothing is stored", () => {
    const input = fakeInput();
    applyWriteOnlySecretState(input, { configured: false, emptyPlaceholder: "Account password" });
    assert.equal(input.required, true);
    assert.equal(input.placeholder, "Account password");
  });

  it("never requires an optional secret, stored or not (the SMTP password)", () => {
    for (const configured of [true, false]) {
      const input = fakeInput();
      applyWriteOnlySecretState(input, { configured, emptyPlaceholder: "leave blank if not required", optional: true });
      assert.equal(input.required, false, `configured=${configured}`);
      assert.equal(input.value, "");
    }
  });

  it("uses the same saved placeholder for every write-only field", () => {
    assert.match(WRITE_ONLY_SAVED_PLACEHOLDER, /^Saved\./);
  });
});
