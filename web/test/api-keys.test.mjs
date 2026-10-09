import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";

import {
  clearTypedKey,
  DECLINE_COOLDOWN_MS,
  resetPromptState,
  fetchProtected,
  getTypedKey,
  isAdminRoute,
  keyForRequest,
  setTypedKey,
} from "../.test-build/api-keys.mjs";

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const insufficient = () => json(403, { detail: "needs admin", required_scope: "admin" });
const unauthorized = () => json(401, { detail: "Invalid or missing X-Domesti-Api-Key" });
const ok = () => json(200, { ok: true });

/** A fake server: `rules` maps (path, key) to a response factory. Records every key sent. */
function harness({ meta = null, answers = [], respond }) {
  const sent = [];
  const prompts = [];
  const remaining = [...answers];
  return {
    sent,
    prompts,
    deps: {
      fetchImpl: async (path, init) => {
        const key = new Headers(init.headers).get("X-Domesti-Api-Key");
        sent.push([path, key]);
        return respond(path, key);
      },
      metaKey: () => meta,
      promptForKey: async (message) => {
        prompts.push(message);
        return remaining.length ? remaining.shift() : null;
      },
    },
  };
}

beforeEach(() => {
  clearTypedKey();
  resetPromptState();
});

describe("route classification", () => {
  it("treats settings, execute-line and the My Tracks sync routes as admin routes", () => {
    for (const path of [
      "/v1/settings",
      "/v1/settings/smtp",
      "/v1/settings/my-tracks/pair?x=1",
      "/v1/execute-line",
      "/v1/rules/geofences/sync",
      "/v1/rules/users/sync",
    ]) {
      assert.equal(isAdminRoute(path), true, path);
    }
  });

  it("does not treat device control, rules reads or look-alike paths as admin routes", () => {
    for (const path of [
      "/v1/ui/state",
      "/v1/ui/global/bulk-off",
      "/v1/rules",
      "/v1/rules/geofences",
      "/v1/rules/geofences/sync-status",
      "/v1/settingsx",
      "/v1/meta",
    ]) {
      assert.equal(isAdminRoute(path), false, path);
    }
  });
});

describe("key selection", () => {
  it("sends the meta (control) key on ordinary routes and never the typed key when a meta key exists", () => {
    setTypedKey("admin-typed");
    assert.equal(keyForRequest("/v1/ui/state", "meta-control"), "meta-control");
  });

  it("sends the typed key on admin routes, falling back to the meta key when none was typed", () => {
    assert.equal(keyForRequest("/v1/settings/smtp", "meta-control"), "meta-control");
    setTypedKey("admin-typed");
    assert.equal(keyForRequest("/v1/settings/smtp", "meta-control"), "admin-typed");
  });

  it("uses the typed key everywhere when there is no meta key (admin-only deployments)", () => {
    setTypedKey("admin-typed");
    assert.equal(keyForRequest("/v1/ui/state", null), "admin-typed");
    assert.equal(keyForRequest("/v1/settings/smtp", null), "admin-typed");
  });

  it("trims the typed key and treats blank as none", () => {
    setTypedKey("  k  ");
    assert.equal(getTypedKey(), "k");
    setTypedKey("   ");
    assert.equal(getTypedKey(), null);
  });
});

describe("fetchProtected", () => {
  it("does not prompt when the meta key already satisfies the route (single-key deployments)", async () => {
    const h = harness({ meta: "ctl", respond: () => ok() });
    const res = await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.equal(res.status, 200);
    assert.deepEqual(h.prompts, []);
    assert.deepEqual(h.sent, [["/v1/settings/smtp", "ctl"]]);
  });

  it("prompts on a 403 naming the admin scope, retries with the typed key and keeps it for later calls", async () => {
    const h = harness({
      meta: "ctl",
      answers: ["adm"],
      respond: (_path, key) => (key === "adm" ? ok() : insufficient()),
    });
    const res = await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.equal(res.status, 200);
    assert.equal(h.prompts.length, 1);
    assert.deepEqual(h.sent, [
      ["/v1/settings/smtp", "ctl"],
      ["/v1/settings/smtp", "adm"],
    ]);
    const again = await fetchProtected("/v1/settings/my-tracks", { method: "GET" }, h.deps);
    assert.equal(again.status, 200);
    assert.equal(h.prompts.length, 1, "the typed key is reused without asking again");
  });

  it("never sends the typed admin key to ordinary routes while a meta key exists", async () => {
    setTypedKey("adm");
    const h = harness({ meta: "ctl", respond: () => ok() });
    await fetchProtected("/v1/ui/state", { method: "GET" }, h.deps);
    assert.deepEqual(h.sent, [["/v1/ui/state", "ctl"]]);
  });

  it("returns the original 403 when the prompt is declined", async () => {
    const h = harness({ meta: "ctl", answers: [null], respond: () => insufficient() });
    const res = await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.equal(res.status, 403);
    assert.equal(h.sent.length, 1);
    assert.equal(getTypedKey(), null);
  });

  it("clears a rejected typed key on 401 and asks again, then gives up after two prompts", async () => {
    const h = harness({ meta: "ctl", answers: ["wrong1", "wrong2"], respond: () => unauthorized() });
    const res = await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.equal(res.status, 401);
    assert.equal(h.prompts.length, 2);
    assert.match(h.prompts[1], /not accepted/);
    assert.equal(getTypedKey(), "wrong2", "the last typed key stays until the server accepts or rejects it again");
    assert.equal(h.sent.length, 3);
  });

  it("a 403 for another scope does not prompt", async () => {
    const h = harness({
      meta: "ctl",
      answers: ["adm"],
      respond: () => json(403, { detail: "x", required_scope: "control" }),
    });
    const res = await fetchProtected("/v1/ui/state", { method: "GET" }, h.deps);
    assert.equal(res.status, 403);
    assert.deepEqual(h.prompts, []);
  });

  it("a 401 on an ordinary route with a meta key does not prompt (the page key is bad, not the admin key)", async () => {
    const h = harness({ meta: "ctl", answers: ["adm"], respond: () => unauthorized() });
    const res = await fetchProtected("/v1/ui/state", { method: "GET" }, h.deps);
    assert.equal(res.status, 401);
    assert.deepEqual(h.prompts, []);
  });

  it("admin-only deployment: no meta key, the first protected request prompts and the key is used everywhere", async () => {
    const h = harness({
      meta: null,
      answers: ["adm"],
      respond: (_path, key) => (key === "adm" ? ok() : unauthorized()),
    });
    const first = await fetchProtected("/v1/ui/state", { method: "GET" }, h.deps);
    assert.equal(first.status, 200);
    const second = await fetchProtected("/v1/ui/global/bulk-off", { method: "POST" }, h.deps);
    assert.equal(second.status, 200);
    const third = await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.equal(third.status, 200);
    assert.equal(h.prompts.length, 1);
    assert.deepEqual(
      h.sent.map(([, key]) => key),
      [null, "adm", "adm", "adm"],
    );
  });

  it("open mode: nothing to prompt for when the server answers 200 without any key", async () => {
    const h = harness({ meta: null, respond: () => ok() });
    const res = await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.equal(res.status, 200);
    assert.deepEqual(h.prompts, []);
    assert.deepEqual(h.sent, [["/v1/settings/smtp", null]]);
  });

  it("keeps method and body across the retry", async () => {
    const bodies = [];
    const deps = {
      fetchImpl: async (_path, init) => {
        bodies.push([init.method, init.body]);
        return bodies.length === 1 ? insufficient() : ok();
      },
      metaKey: () => "ctl",
      promptForKey: async () => "adm",
    };
    const res = await fetchProtected("/v1/settings/smtp", { method: "PUT", body: '{"a":1}' }, deps);
    assert.equal(res.status, 200);
    assert.deepEqual(bodies, [
      ["PUT", '{"a":1}'],
      ["PUT", '{"a":1}'],
    ]);
  });

  it("a stale 401 for a request sent before the key was typed does not wipe the typed key or prompt again", async () => {
    // Admin-only deployment: two requests go out with no key; the first answer prompts and the user types
    // the right key; the second (late) 401 refers to the request that carried no key.
    const sentByRequest = { a: [], b: [] };
    let releaseB;
    const bGate = new Promise((resolve) => {
      releaseB = resolve;
    });
    let prompts = 0;
    const mk = (name, gate) => ({
      fetchImpl: async (_path, init) => {
        const key = new Headers(init.headers).get("X-Domesti-Api-Key");
        sentByRequest[name].push(key);
        if (gate && key === null) {
          await gate;
        }
        return key === "adm" ? ok() : unauthorized();
      },
      metaKey: () => null,
      promptForKey: async () => {
        prompts += 1;
        return "adm";
      },
    });
    const a = fetchProtected("/v1/ui/state", { method: "GET" }, mk("a"));
    const b = fetchProtected("/v1/settings/smtp", { method: "GET" }, mk("b", bGate));
    assert.equal((await a).status, 200);
    releaseB();
    assert.equal((await b).status, 200);
    assert.equal(prompts, 1, "only one prompt for two concurrent unauthenticated requests");
    assert.deepEqual(sentByRequest.b, [null, "adm"], "the late request retried with the typed key");
    assert.equal(getTypedKey(), "adm");
  });

  it("after the prompt is declined, other requests do not re-prompt until the cooldown passes", async () => {
    let clock = 1_000;
    const h = harness({ meta: "ctl", answers: [null, "adm"], respond: (_p, key) => (key === "adm" ? ok() : insufficient()) });
    h.deps.now = () => clock;
    const first = await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.equal(first.status, 403);
    assert.equal(h.prompts.length, 1);

    clock += DECLINE_COOLDOWN_MS - 1;
    const quiet = await fetchProtected("/v1/settings/my-tracks", { method: "GET" }, h.deps);
    assert.equal(quiet.status, 403);
    assert.equal(h.prompts.length, 1, "no new prompt during the cooldown");

    clock += 2;
    const later = await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.equal(later.status, 200);
    assert.equal(h.prompts.length, 2, "prompts again once the cooldown has passed");
  });

  it("a 401 on an admin route says the admin key may be the control key when no admin key is configured", async () => {
    const h = harness({ meta: "ctl", answers: [null], respond: () => unauthorized() });
    await fetchProtected("/v1/settings/smtp", { method: "GET" }, h.deps);
    assert.match(h.prompts[0], /admin key, or the control key/);
  });

  it("a 401 for the typed key during the decline cooldown still drops the rejected key", async () => {
    let clock = 5_000;
    const h = harness({ meta: null, answers: [null, "bad"], respond: () => unauthorized() });
    h.deps.now = () => clock;
    await fetchProtected("/v1/ui/state", { method: "GET" }, h.deps); // prompt declined
    assert.equal(h.prompts.length, 1);

    setTypedKey("stale-typed-key"); // e.g. typed by a concurrent request, then rejected by the server
    clock += 1_000;
    const res = await fetchProtected("/v1/ui/state", { method: "GET" }, h.deps);
    assert.equal(res.status, 401);
    assert.equal(h.prompts.length, 1, "no prompt during the cooldown");
    assert.equal(getTypedKey(), null, "the rejected typed key is cleared");
  });
});
