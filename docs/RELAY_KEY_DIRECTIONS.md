# Relay key: one credential per direction

**Status:** proposed design, no code in this document's PR. Tracks item 9 of the write-only secrets stack (domesti-bot#727). Implementing it needs a coordinated change in domesti-bot and my-tracks, outlined below. The design was reviewed against the code of both repos before this version; the corrections it prompted are folded in.

## How the relay key works today

domesti-bot generates one random key (`secrets.token_urlsafe(32)`, 256 bits) when the operator pairs and sends it to My Tracks in `POST /api/admin/domesti-bot/pair/` (field `api_key`). From then on both services use that same value as the `X-Domesti-Api-Key` header, in opposite directions.

| Direction | Who presents the key | Who verifies it | Code |
| --- | --- | --- | --- |
| My Tracks to domesti-bot (location webhooks, including the admin "test location update") | My Tracks `send_location_webhook` (called from `domesti_relay.py` by the MQTT plugin) | domesti-bot `verify_mytracks_relay_api_key` | `app/api/mytracks_relay_auth.py` in domesti-bot |
| domesti-bot to My Tracks (request-location) | domesti-bot `request_user_location` | My Tracks `DomestiRelayApiKeyPermission` | `app/mytracks_service.py` in domesti-bot, `app/domesti_bot_auth.py` in my-tracks |

Both services store the key reversibly. domesti-bot keeps it Fernet-encrypted in the `app_secrets` row `mytracks_relay_api_key`, with the Fernet key in `domesti-bot.config.json` at the repo root or in `DOMESTI_BOT_SECRETS_KEY`. My Tracks keeps it in `DomestiBotConfig.encrypted_api_key`, encrypted with a key derived from the Django `SECRET_KEY`, which is also the session and CSRF signing key. Each side decrypts the key to present it and to compare an incoming key against it.

The request-location direction has two further gates besides the key: the pairing must be complete (`is_paired`) and `remote_request_location_enabled` must be on. `DomestiRelayApiKeyPermission` guards only the two request-location views, so that key already authorizes nothing else.

My Tracks also exposes `GET /api/admin/domesti-bot/reveal-api-key/`, a staff-only endpoint that returns the decrypted key. Its only consumer is the eye toggle in the Admin panel.

Two behaviors matter for any change to the exchange:

- My Tracks sends each location webhook once, with a 10 second timeout, and logs and swallows a failure. It does not retry, so a webhook that fails during a rotation is dropped, not retried.
- My Tracks commits the new key inside the pair call, before it responds, while domesti-bot stores its copy only after the response (the order layer 8 of this stack implements, so a failed pair never replaces a working key). Between those two moments the two sides disagree.

## Why this is worth changing

- A service that only verifies a key does not need to be able to recover it. A common practice for API keys is to store a hash of a key the server only verifies, so a leaked database copy holds no usable credential. Stripe and GitHub are commonly described as doing this (a secondary source: [apikeys.guide on hashing and storage](https://apikeys.guide/docs/security/hashing-and-storage); there is no primary specification).
- Here each side verifies one direction but holds the key for both. An attacker who obtains domesti-bot's database and its Fernet key gets the credential My Tracks presents to domesti-bot, which authorizes location posts. Those posts drive home automations, so forging them is the highest-value abuse of the pairing.
- The My Tracks `reveal-api-key` endpoint is a readback of the kind the write-only secrets stack removes from domesti-bot.
- One shared secret means a leak on either side compromises both directions, and rotation has no overlap, so locations arriving during the swap are dropped.
- There is a pre-existing bug on the My Tracks side: `DomestiRelayApiKeyPermission` calls `secrets.compare_digest` on `str` values, which raises `TypeError` for a non-ASCII `X-Domesti-Api-Key`, so such a request gets a 500 instead of a 401. Comparing digests of the encoded values fixes it.

## Honest limits of hashing

Hashing the verifier protects data at rest. It does not help against an attacker who sees the key in use.

- An attacker who reads a backup or the database file, but not the key store, gets nothing useful today because Fernet protects the key; a digest changes little for that case.
- An attacker on the same host or with the repo root gets the database and the Fernet key together (domesti-bot keeps its Fernet key in a gitignored file beside the code and the database cache; My Tracks derives its key from `SECRET_KEY`). Today that yields the key. With a digest-only verifier it does not, which is the real gain.
- An attacker who can read process memory or sniff traffic sees the presented key in plaintext on every request, in either design. The transport must be TLS; both services currently accept `http://` base URLs, so this is an assumption to state and ideally enforce.
- An attacker who can write the database can replace a stored digest with the digest of a key they chose and authenticate, which needs no Fernet key. Today they cannot craft valid ciphertext without it. A plain digest is therefore weaker than ciphertext for integrity when the Fernet key is kept away from the database. Storing an HMAC-SHA-256 keyed with a pepper held beside the Fernet key restores that property, because a forged verifier cannot be computed without the pepper. This, not offline guessing of 256-bit keys, is the real case for a pepper.

## Goals and non-goals

- Goal: each direction has its own random key, and the service that only verifies a direction's key stores only a verifier for it.
- Goal: no endpoint on either service returns a stored key.
- Goal: rotation without a window where one side rejects the other, and without leaving an old key valid longer than needed.
- Goal: old and new builds keep working together, with the active mode visible, and an operator moves to the new protocol by re-pairing once.
- Non-goal: mutual TLS, OAuth client credentials or signed requests. They could be layered on later but are a different project.
- Non-goal: changing how the operator authenticates to either service.

## Proposed protocol (version 2)

Two keys, named from domesti-bot's point of view and always called by that name in both repos:

- `K_in` is the key My Tracks presents to domesti-bot (inbound to domesti-bot).
- `K_out` is the key domesti-bot presents to My Tracks (outbound from domesti-bot).

domesti-bot generates both. It already generates the single key today, so a second one costs nothing, and generating both on one side removes any dependency on a value returned once in a response that could be lost.

| Item | domesti-bot stores | My Tracks stores |
| --- | --- | --- |
| `K_in` (My Tracks presents, domesti-bot verifies) | a verifier only: an HMAC-SHA-256 digest, so the key cannot be recovered | ciphertext, because My Tracks must present it |
| `K_out` (domesti-bot presents, My Tracks verifies) | ciphertext, because domesti-bot must present it | a verifier only: an HMAC-SHA-256 digest |

Each service holds one verifier and one credential to present. Stealing a service's database and key store no longer yields the key that authenticates traffic into that same service.

Pairing is a two-phase stage, probe and activate exchange. Both sides keep their active keys working until the new ones are confirmed, and neither side switches on its own:

1. **Stage on domesti-bot.** domesti-bot generates `K_in` and `K_out` and a `pairing_id` (a random nonce), and writes them as pending: a pending `K_in` verifier and a pending encrypted `K_out`, each with an expiry (proposed: 30 minutes). Pending values never replace the active ones. While a pending `K_in` verifier exists, domesti-bot's webhook verifier accepts the active or the pending `K_in`.
2. **Stage on My Tracks.** domesti-bot sends `POST /api/admin/domesti-bot/pair/` with the existing fields plus `protocol_version: 2`, `pairing_id`, `api_key` carrying `K_in` and a new `outbound_api_key` carrying `K_out`. My Tracks, seeing version 2, stores `K_in` encrypted and the verifier of `K_out` as pending too, also with an expiry. It does **not** switch: it keeps presenting its active `K_in` and keeps verifying its active `K_out`. It replies with `protocol_version` set to the version it chose, the lower of what was requested and what it supports.
3. **Probe both directions with the pending keys.** domesti-bot triggers the My Tracks test-location-update with the pairing id, and My Tracks presents the pending `K_in` to domesti-bot's test URL (accepted because domesti-bot holds the pending verifier). domesti-bot also calls a new My Tracks auth-check endpoint with the pending `K_out` and the pairing id (My Tracks accepts a pending key only on the auth-check, never on request-location, which queues a command and is not used as a probe).
4. **Activate.** Only if both probes succeed does domesti-bot enter the `activating` state and send `POST /api/admin/domesti-bot/pair/activate/` with the pairing id. My Tracks atomically promotes its pending keys to active and keeps the previous ones for a short grace (about one webhook timeout, a minute at most) for requests already in flight. Activation is idempotent for a given pairing id, so domesti-bot may retry it when the response is lost.
5. **Promote on domesti-bot.** After My Tracks acknowledges the activation (or reports the pairing active when asked), domesti-bot promotes its pending values to active and clears the previous ones after the same grace.
6. **Abort and expiry apply only to pairings known not to have activated.** domesti-bot tracks each pairing as `staged`, `probing`, `activating`, `active` or `aborted`. While `staged` or `probing`, any failure deletes the pending values and sends a best-effort `POST /api/admin/domesti-bot/pair/abort/`; if that is lost, the pending values expire (proposed: 30 minutes) on both sides and the previous pairing is simply still active. From the moment domesti-bot sends the activation it is `activating`: the outcome on My Tracks is unknown until My Tracks answers, so domesti-bot must keep its pending `K_in` verifier and encrypted `K_out`, suspend their expiry, and keep reconciling (retrying the idempotent activation and querying the pairing's state, with backoff) until My Tracks reports `active` (promote) or confirms it never activated (abort). If My Tracks stays unreachable the pairing stays `activating`, visible in `pair-status` and the panel as an unconfirmed pairing, and the operator can re-pair; nothing is deleted on a guess. Nothing ever has to be restored in the other cases, because nothing was switched.
7. Both headers stay `X-Domesti-Api-Key`; the value differs per direction.

Why the activation handshake exists: without it My Tracks would start presenting the new `K_in` as soon as it stored it, before domesti-bot had confirmed anything. A failed probe or a lost response would then leave My Tracks sending a key domesti-bot discards, and because My Tracks sends each webhook once and does not retry, those locations would be dropped until the next pairing. With staged keys the worst outcomes are an expired pending pairing (retry) and a few seconds of overlap around activation, during which domesti-bot accepts both its active and its pending `K_in`.

Failure cases the protocol has to survive, each ending with the previous pairing still working or the new one fully active:

- the stage response is lost: My Tracks holds a pending pairing, domesti-bot does not know; domesti-bot stages again with a new pairing id, which replaces the pending one;
- a probe fails: nothing was activated; abort or expiry clears the pending values;
- the activate response is lost: domesti-bot is `activating`, keeps its pending values, retries with the same pairing id and My Tracks answers that it is already active, whereupon domesti-bot promotes; it never deletes pending values or lets them expire while the outcome is unknown;
- domesti-bot crashes or restarts while `activating`, including after what would have been the pending expiry: it finds the pairing in `activating`, asks My Tracks for the pairing's state, promotes if My Tracks reports it active and aborts only if My Tracks confirms it never activated;
- My Tracks is unreachable at activation: the pairing stays `activating` and unconfirmed, with its pending values retained and the previous pairing still valid on My Tracks until it answers; it is retried and shown to the operator, and never discarded unconfirmed.

Verification on both sides compares fixed-size digests with `hmac.compare_digest`, after hashing the presented value encoded as UTF-8. That removes the length leak and fixes the non-ASCII 500. A slow hash such as argon2 is the wrong tool: webhooks arrive for every location update, so a slow hash on unauthenticated requests would be a denial-of-service lever, and the keys are 256-bit random values with nothing to brute-force. The generator must keep producing at least 32 random bytes, and the verifier should reject a presented value outside a sane length range.

## Negotiation and downgrade

The version is chosen at pairing, so nothing breaks when one side is upgraded first.

| domesti-bot | My Tracks | Result |
| --- | --- | --- |
| new | new | version 2 as above |
| new | old | the old pair view ignores the new fields and does not echo `protocol_version`, so domesti-bot falls back to version 1 and keeps one reversible key for both directions, exactly as today |
| old | new | the pair request has no `protocol_version`, so My Tracks stores and checks the key as today and issues nothing new |
| old | old | unchanged |

Falling back silently would hide the loss of the digest property, so the fallback must be visible and optional:

- domesti-bot records the active version per pairing and exposes it in `pair-status` and the panel, with a warning when it is 1.
- A setting "require version 2" makes domesti-bot refuse to pair unless My Tracks echoes version 2. The downgrade cost is bounded: a compromised or downgraded My Tracks already knows `K_in`, so the loss is the at-rest property, and an attacker who can alter the response can already read the request unless TLS is in use.
- My Tracks shows the active version and the key's generation time in its Admin panel too.

## Storage and rollback

Version 2 uses new storage names and leaves the version 1 ones untouched.

- domesti-bot: new `app_secrets` rows for the inbound verifier, the pending inbound verifier, the outbound key and the pending outbound key, plus a version flag per pairing. The existing `mytracks_relay_api_key` row is not reused for `K_out`.
- My Tracks: new columns for the active outbound verifier, its previous value with an expiry, a pending `K_in` (encrypted) and pending outbound verifier with the pairing id and an expiry, and the version, added by a reversible migration whose defaults keep existing paired rows at version 1. The existing `encrypted_api_key` keeps holding the active `K_in` in version 2 and the single key in version 1.

Reusing the old names would break a rollback: an older domesti-bot reading `K_out` from the old row would use it to verify webhooks, which carry `K_in`, and reject all of them. After a rollback of either service the operator re-pairs; nothing else is required.

The previous verifier lives next to the current one with an expiry that is checked at verify time. Rotating twice quickly overwrites the single previous slot with the first rotation's key, so at most the current, the pending and one previous key are ever live per direction.

## Removing readbacks

- domesti-bot already returns no key (layer 8).
- My Tracks `reveal-api-key` is removed in both modes, together with the eye-toggle JavaScript and CSS in the Admin panel, the URL, the view and its test. It has no other consumer, so this part is independent of version 2 and can ship first. The Admin panel then shows that a key is configured, when it was generated and the protocol version, and tells the admin to re-pair to rotate it.

## What an attacker gets

| Compromised | Today | Version 2 |
| --- | --- | --- |
| domesti-bot database and Fernet key | the single key: forge location posts into domesti-bot and call My Tracks request-location | `K_out` only: call My Tracks request-location. Cannot forge locations, because only a verifier of `K_in` is stored |
| My Tracks database and `SECRET_KEY` | the single key: forge location posts and call request-location | `K_in` only: forge location posts into domesti-bot. Cannot call request-location, because only a verifier of `K_out` is stored |
| domesti-bot database and Fernet key, when the operator saved the My Tracks admin password | the single key and the admin password | `K_out` and the admin password. The admin password dominates: it allows reading all location history, the sync export and re-pairing My Tracks with any key. This design does not address it |
| write access to a service's database only | cannot forge ciphertext without the key | with a plain digest, can substitute a verifier and authenticate; with the keyed HMAC verifier, cannot |
| memory read or network sniffing on either side | the presented key | the presented key; hashing does not help, TLS is required |

Each database compromise still yields one credential, never both.

## Implementation outline

domesti-bot:

- Generate and store the keys as described, with the stage, probe, activate and promote flow, the pairing id and expiry, the `activating` state with its reconciliation (retry and state query, no expiry or abort while the outcome is unknown), the version flag per pairing and the "require version 2" setting.
- `pair_with_my_tracks` sends `protocol_version` and `outbound_api_key` and reads the echoed version; `request_user_location` and the coordinator present `K_out` in version 2 and the single key in version 1.
- `verify_mytracks_relay_api_key` compares keyed digests, accepting the current, the pending and an unexpired previous `K_in` verifier.
- `pair-status`, the TypeScript types and the panel report the active version, `relay_key_updated_at` and whether a pairing is pending or unconfirmed.
- `clear_mytracks_pairing` deletes every new row. The audit log records created, replaced and removed for the new rows by name, never a value; a verifier row is logged like any other secret row, which is intended.
- Update the dev scripts `scripts/internal/verify-mytracks-pairing` and `verify-mytracks-location-request`, `docs/AGENTS.md` and the integration plan, and keep the explicit timeouts and bounded retries the repository rules require on the pair call; the pair call must not be retried unless it is made idempotent.
- The pair request carries both plaintext keys, so it must never be logged, and the pair response keeps `Cache-Control: no-store`; add a log-redaction test.

my-tracks:

- `DomestiBotConfig` gains the outbound verifier, its previous value with an expiry and the protocol version, with a reversible migration.
- `DomestiBotPairView` accepts `protocol_version`, `pairing_id` and `outbound_api_key`, stores them as pending and echoes the chosen version; new `pair/activate/` (idempotent per pairing id), `pair/abort/` and a pairing state query, with pending expiry enforced at use time.
- `DomestiRelayApiKeyPermission` compares keyed digests in version 2 and keeps the decrypt-and-compare path for version 1, and no longer raises on a non-ASCII value.
- A new auth-check endpoint guarded by the same key permission, without the request-location opt-in gate, that also accepts a pending `K_out` for the matching pairing id, so domesti-bot can verify it without queuing a command or activating anything.
- Remove `reveal-api-key` and update the Admin panel, its tests and the integration plan documents.
- A test that fails if `DomestiRelayApiKeyPermission` is attached to any route other than the request-location views and the auth-check.

## Test plan

- Each side rejects the key presented in the wrong direction (`K_in` against My Tracks, `K_out` against domesti-bot).
- A database dump of the verifying side contains no value that authenticates (only verifiers and the ciphertext of the other key).
- The compatibility matrix: new/new, new/old and old/new pair and exchange both directions, and "require version 2" refuses the downgrade.
- Failure and rotation, with a fault injected at every step: a lost stage response, a failed probe in each direction, a lost activate response (retried with the same pairing id), a crash or restart while `activating` (including past the nominal pending expiry), an unreachable My Tracks at activation and an expired pending pairing that never reached activation each leave the previous pairing working or the new one fully active, and never a half state; the previous key stops working after activation plus the grace, or immediately with the revoke option; two quick re-pairs never leave an extra live key.
- Comparison is constant-time and a non-ASCII or oversized header gives 401, not 500.

## Rollout

The two services pair with no atomic switch, so roll out in this order and check as you go.

1. Ship the My Tracks changes that are independent and safe first: removing `reveal-api-key`, the non-ASCII fix and the auth-check endpoint.
2. Upgrade both services to version 2 capable builds. Nothing changes until the operator re-pairs.
3. Re-pair once. domesti-bot stages the new keys, probes both directions and only then activates them, so a failed re-pair leaves the old pairing working on both sides, and the panel shows the result.
4. If something goes wrong, roll the affected service back and re-pair, because version 2 storage does not touch the version 1 rows.

## Open questions

- Should the verifier be the keyed HMAC with a pepper beside the Fernet key (recommended, for write integrity), or a plain digest to avoid managing a second secret?
- Should a compromise-driven rotation have an explicit "revoke the previous key now" action (recommended), and what grace should a normal rotation use?
- Should "require version 2" default to on once both services have shipped it, and when can version 1 be dropped?
- Should `K_out` be scoped further than request-location plus the auth-check, for example per user or per rule?
- Should both services enforce `https://` base URLs for the pairing, since the keys travel in headers and, at pairing, in a request body?
