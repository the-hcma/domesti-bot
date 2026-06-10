# Plan: my-tracks integration (domesti-bot companion)

This document is the **domesti-bot** side of integrating with [my-tracks](https://github.com/the-hcma/my-tracks). The my-tracks companion plan lives at `docs/DOMESTI_BOT_INTEGRATION_PLAN.md` in that repository.

**Rule evaluation, geofence definitions, and device actions stay in domesti-bot.** my-tracks remains the location ingest and map service. After pairing, my-tracks **pushes** live GPS fixes to domesti-bot; roster and geofence **definitions** are still pulled manually by domesti-bot (no my-tracks push webhooks for those).

**Status:** planning — pairing UI and webhook ingest are not implemented yet. Manual participant/geofence sync and My Tracks settings (domain + admin username) already ship.

---

## Responsibilities split

| Concern | Owner | Mechanism |
| --- | --- | --- |
| OwnTracks ingest, map, friends | my-tracks | Existing MQTT/HTTP → SQLite |
| Participant roster (automation) | my-tracks (source of truth) | **Manual pull** by domesti-bot (`POST /v1/rules/participants/sync`) |
| Geofence definitions (automation) | domesti-bot | **Manual pull** (`POST /v1/rules/geofences/sync`) from my-tracks export APIs |
| Live GPS fixes for rules | my-tracks → domesti-bot | **Automatic push** after pairing (`POST` to domesti-bot webhook URLs) |
| Rule evaluation & device actions | domesti-bot | `RuleEvaluator` (future PRs; see `docs/RULE_ENGINE_PLAN.md`) |

---

## HTTPS, public URLs, and secrets

Both services are reached at **HTTPS public endpoints** in production (e.g. `https://tracks.example.com`, `https://domesti.example.com`). LAN `http://` addresses are dev-only.

Pairing does **not** require deploy-time env vars for URLs or the my-tracks relay secret. The operator supplies URLs in the pairing UI; domesti-bot generates and persists the relay secret.

| Item | Who provides it | When |
| --- | --- | --- |
| **My Tracks base URL** | Operator | Pairing form (HTTPS) |
| **domesti-bot public base URL** | Operator confirms | Pairing form, pre-filled from browser origin or reverse-proxy `Forwarded` headers |
| **Relay API key** | domesti-bot generates | At pair (or re-pair); stored encrypted in SQLite; sent once to my-tracks |
| **Participant location update URL** | domesti-bot derives | `{domesti_public_base_url}/v1/webhooks/presence` — registered on my-tracks at pair |
| **Participant location test URL** | domesti-bot derives | `{domesti_public_base_url}/v1/webhooks/presence/test` — registered on my-tracks at pair |

Listen bind (`--listen-all`, port 8003, reverse proxy, TLS termination) is orthogonal — webhooks and pairing always use the **public** HTTPS origin from the pairing form, not the internal bind address.

### Relay secret (my-tracks webhooks only)

domesti-bot is the **source of truth** for the relay secret. my-tracks only receives it at pair time.

1. On **Pair** / **Re-pair**, domesti-bot generates a new key (`secrets.token_urlsafe(32)`).
2. domesti-bot stores it encrypted in `app_secrets` under e.g. `mytracks_relay_api_key` (Fernet via `app/db/secrets.py`, same pattern as Tailwind token).
3. domesti-bot POSTs the plaintext key once to my-tracks `POST /api/admin/domesti-bot/pair/`.
4. my-tracks stores its copy encrypted in `DomestiBotConfig`.
5. Incoming `POST /v1/webhooks/presence` and `…/presence/test` validate `X-Domesti-Api-Key` against the **decrypted DB key**, not `DOMESTI_API_KEY`.

Re-pair **rotates** the key in domesti-bot DB and pushes the new value to my-tracks; the previous key is invalid everywhere.

Before the first successful pair, webhook routes return **`401`** (or **`503`** with “not paired”) — never open.

### Operator UI auth (`DOMESTI_API_KEY`) — separate concern

`DOMESTI_API_KEY` (optional env) continues to guard the **human-facing API** (browser UI, settings routes, device tiles). It is **not** the my-tracks relay secret and is **not** sent to my-tracks at pair.

| Secret | Storage | Used by |
| --- | --- | --- |
| Relay API key | `app_secrets` (encrypted), created at pair | my-tracks → domesti-bot webhooks only |
| `DOMESTI_API_KEY` | Environment (optional) | Browser / operator `X-Domesti-Api-Key` on `/v1/ui/*`, settings, rules CRUD, etc. |

Optional override: `DOMESTI_PUBLIC_BASE_URL` env may pre-fill the pairing form when request-derived origin is wrong (misconfigured proxy). It is **not** required for pairing to work.

---

## What domesti-bot already ships

| Feature | API / module |
| --- | --- |
| My Tracks connection settings | `GET/PUT/DELETE /v1/settings/my-tracks` — domain + default admin username |
| Participant roster sync | `POST /v1/rules/participants/sync` + `GET …/sync-status` |
| Geofence sync | `POST /v1/rules/geofences/sync` + `GET …/sync-status` |
| Last-fix storage (bulk on sync) | `app/presence_store.py`, `rule_participant_last_fix` table |
| Participant status for UI | `GET /v1/rules/participants/status` |
| Admin session client to my-tracks | `app/mytracks_service.py` (login + export JSON) |

**Not started:** pairing client, webhook ingest, pairing status UI, emergency switch, verify roundtrip.

---

## Pairing flow (domesti-bot → my-tracks)

Pairing is **initiated from domesti-bot**. The relay secret is **never** typed into my-tracks; domesti-bot generates it, persists it, and pushes it once pairing succeeds.

```mermaid
sequenceDiagram
  participant Op as Operator
  participant Bot as domesti-bot
  participant MT as my-tracks

  Op->>Bot: Settings → My Tracks: My Tracks HTTPS URL + domesti public URL + admin user
  Op->>Bot: Pair (admin password prompt)
  Bot->>Bot: Generate relay_api_key; encrypt → app_secrets
  Bot->>Bot: Build presence + test webhook URLs from public URL
  Bot->>MT: POST /api/admin/domesti-bot/pair/ (admin session)
  Note over Bot,MT: api_key, participant_location_update_url,<br/>participant_location_test_url, domesti_base_url
  MT->>MT: Store DomestiBotConfig, location_updates_enabled=true
  MT-->>Bot: 200 paired
  Bot->>Bot: Persist paired_at + URLs in mytracks_settings
  Op->>Bot: Optional: Sync participants + geofences
  Note over MT,Bot: Later: live fixes → POST …/presence<br/>Tests → POST …/presence/test
```

### domesti-bot pairing endpoint (to implement)

```
POST /v1/settings/my-tracks/pair
Content-Type: application/json
X-Domesti-Api-Key: <UI session key, same as other settings routes>
```

**Request body:**

```json
{
  "domain": "https://tracks.example.com",
  "domesti_public_base_url": "https://domesti.example.com",
  "username": "admin",
  "password": "<admin password, not stored>"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `domain` | yes | Operator-supplied **HTTPS** my-tracks public URL |
| `domesti_public_base_url` | yes | Operator-confirmed **HTTPS** domesti-bot public origin (UI pre-fills from `window.location.origin` or server `Forwarded` headers) |
| `username` | yes | Staff admin username for my-tracks session login |
| `password` | yes | One-shot; used only for the pair request (not stored unless operator opts into “remember admin password” later — out of scope for v1) |

**Preconditions:**

- Discovery cache path must be configured (same as other My Tracks settings).
- Valid Fernet key for `app_secrets` (same as Tailwind / SMTP encrypted storage).
- `domesti_public_base_url` and `domain` must be absolute `https://` URLs in production.

**domesti-bot → my-tracks pair payload:**

```json
{
  "api_key": "<newly generated relay key>",
  "domesti_base_url": "https://domesti.example.com",
  "participant_location_update_url": "https://domesti.example.com/v1/webhooks/presence",
  "participant_location_test_url": "https://domesti.example.com/v1/webhooks/presence/test"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `api_key` | yes | Fresh relay secret; domesti-bot persists encrypted before send; my-tracks stores encrypted; never returned in API responses |
| `domesti_base_url` | yes | From pairing form |
| `participant_location_update_url` | yes | Live GPS relay target |
| `participant_location_test_url` | yes | Synthetic / verify traffic only (see below) |

**Responses:**

- `200` — pair succeeded; body includes `paired_at`, echoed public URLs; `relay_key_configured: true` (never the key itself).
- `400` / `422` — validation (bad URL, missing Fernet key, my-tracks rejected body).
- `502` — my-tracks unreachable or pair endpoint error.

**Side effects:** upsert My Tracks settings (`domain`, `username`, public URL, webhook URLs), encrypt/store relay key in `app_secrets`, set `paired_at`, clear `last_pair_error`.

### my-tracks companion change

PR [the-hcma/my-tracks#1087](https://github.com/the-hcma/my-tracks/pull/1087) adds `participant_location_update_url` only. Before cutover, my-tracks must also accept and persist **`participant_location_test_url`** (same validation: absolute HTTPS URL). my-tracks **Test location update** (P4) and domesti-bot **Verify pairing** must POST to the **test** URL, never the live relay URL.

---

## Webhook endpoints (domesti-bot)

Both routes require `X-Domesti-Api-Key` matching the **relay secret** from `app_secrets` (decrypted at request time). They use a dedicated verifier — **not** the env-based `_verify_api_key` used for operator UI routes. They must **not** depend on device discovery (`_device_state`).

### Live location relay

```
POST /v1/webhooks/presence
```

**Body** (`PresenceWebhookIn`):

```json
{
  "participant_id": "kristen",
  "lat": 41.194085,
  "lon": -73.888365,
  "accuracy_m": 12,
  "timestamp": "2026-06-09T23:14:58Z",
  "source": "my-tracks",
  "device_id": "pixel7pro",
  "mqtt_user": "kristen"
}
```

| Response | When |
| --- | --- |
| `204` | Fix accepted and stored |
| `401` | Not paired, missing header, or relay key mismatch |
| `404` | Unknown `participant_id` (roster not synced) |
| `422` | Bad coordinates or timestamp |
| `503` | **Emergency switch** — domesti-bot is dropping location updates (see below) |

**Side effects:**

- Upsert `rule_participant_last_fix` for `participant_id`.
- Future: `RuleEvaluator.request_evaluation(reason="presence")`.

Honour `timestamp` when present (ISO-8601 UTC preferred); optional ignore of duplicate/older fixes.

### Test location relay

```
POST /v1/webhooks/presence/test
```

Same request body and auth as live relay. Differences:

| Aspect | Live `/presence` | Test `/presence/test` |
| --- | --- | --- |
| Trigger | my-tracks on each saved GPS fix | my-tracks P4 test button; domesti-bot verify roundtrip |
| Respects emergency switch | yes (`503`) | **no** — verify must still work when live ingest is paused |
| Persists fix | yes | **no** — validate auth + schema + known participant; return `204` without writing (or write to a non-evaluated “test receipt” log only) |
| Evaluator | future: schedule | never |

This keeps production rule state clean while operators confirm connectivity.

### Debug ingest (optional, same PR as live relay)

```
PUT /v1/presence/{participant_id}
```

Same fields as webhook body for curl/manual debugging; same responses as live relay.

---

## Pairing configuration tab (Settings → My Tracks)

Extend the existing My Tracks settings panel into a **pairing configuration** surface.

### Not paired

- Fields: **My Tracks URL** (HTTPS), **domesti-bot public URL** (HTTPS, pre-filled), **default admin username**, Save.
- **Pair** button → password prompt → `POST /v1/settings/my-tracks/pair`.
- Status badge: **Not paired**.
- Help: pairing generates a relay secret, registers domesti-bot’s public webhook URLs and that secret on my-tracks; run participant and geofence sync afterward.

### Paired

- Status badge: **Paired** with `paired_at` (local + optional my-tracks confirmation via `GET /api/admin/domesti-bot/config/`).
- Read-only summary:
  - domesti-bot public base URL
  - Live webhook URL (`…/v1/webhooks/presence`)
  - Test webhook URL (`…/v1/webhooks/presence/test`)
  - Relay secret: **configured** (never shown; rotated on re-pair)
- **Re-pair** — same flow as Pair; generates a new relay key, updates DB, pushes to my-tracks.
- **Verify pairing** — roundtrip check (below).
- **Emergency: drop location updates** — toggle (below).
- Links to Automations hub for participant/geofence sync (unchanged manual pulls).

### Pairing status API (to implement)

```
GET /v1/settings/my-tracks/pair-status
```

```json
{
  "paired_at": "2026-06-10T14:00:00Z",
  "domain": "https://tracks.example.com",
  "username": "admin",
  "domesti_public_base_url": "https://domesti.example.com",
  "participant_location_update_url": "https://domesti.example.com/v1/webhooks/presence",
  "participant_location_test_url": "https://domesti.example.com/v1/webhooks/presence/test",
  "relay_key_configured": true,
  "location_updates_accepted": true,
  "mytracks_location_updates_enabled": true,
  "last_verify_at": "2026-06-10T14:05:00Z",
  "last_verify_ok": true
}
```

`mytracks_location_updates_enabled` is read from my-tracks `GET /api/admin/domesti-bot/config/` when paired (best-effort cache).

---

## Verify pairing (roundtrip)

**UI:** “Verify pairing” on the paired configuration tab.

**Goal:** Confirm my-tracks can reach domesti-bot with the stored relay secret, without writing a live fix or firing rules.

**Flow (domesti-bot orchestrates):**

1. Authenticate to my-tracks (admin session, saved domain + username + password prompt).
2. Call my-tracks `POST /api/admin/domesti-bot/test-location-update/` with optional `participant_id` (defaults to first roster member after sync).
3. my-tracks POSTs a **synthetic** payload to the stored **`participant_location_test_url`** (`…/presence/test`), not the live URL.
4. domesti-bot test handler validates and returns `204`.
5. my-tracks returns success/failure to domesti-bot; domesti-bot surfaces result in UI (toast + `last_verify_at` / `last_verify_ok`).

If step 2 is unavailable (older my-tracks), fallback: domesti-bot `POST`s synthetic body to its own test URL internally and reports “local handler OK” only — document as degraded verify.

---

## Emergency switch: drop location updates

**UI:** Toggle on pairing configuration tab — e.g. **“Accept location updates”** (on by default).

Two layers (operator sees one toggle; implementation applies both):

| Layer | Mechanism | Effect |
| --- | --- | --- |
| **domesti-bot (local)** | `location_updates_accepted` in SQLite | Live `POST /v1/webhooks/presence` returns **`503`** with `Retry-After`; fixes not stored; evaluator not scheduled |
| **my-tracks (upstream)** | `PATCH /api/admin/domesti-bot/config/` with `{ "location_updates_enabled": false }` | my-tracks stops sending live relays (ingest unaffected) |

Turning the switch **off** sets both. Turning **on** re-enables both (PATCH `true`). Test URL and **Verify pairing** remain available when live ingest is paused.

```
PATCH /v1/settings/my-tracks/location-updates
{ "accepted": false }
```

Requires paired state; returns updated `pair-status` shape.

---

## Manual sync (unchanged)

After pairing, operators still run **Sync from my-tracks** in the Automations hub (participants + geofences). my-tracks does not push roster or geofence changes.

Participant sync must run **before** live webhooks succeed (`404` until `participant_id` exists in domesti-bot).

---

## Persistence (domesti-bot SQLite)

### `mytracks_settings` (additive migration)

| Column | Purpose |
| --- | --- |
| `paired_at` | Last successful pair (epoch) |
| `last_pair_error` | Optional operator-visible failure |
| `domesti_public_base_url` | Public HTTPS origin from pairing form |
| `participant_location_update_url` | Live webhook URL sent at pair |
| `participant_location_test_url` | Test webhook URL sent at pair |
| `location_updates_accepted` | Emergency switch (default `true`) |
| `last_verify_at` / `last_verify_ok` | Last verify roundtrip result |

### `app_secrets` (encrypted)

| Key | Purpose |
| --- | --- |
| `mytracks_relay_api_key` | Relay secret generated at pair; validated on webhook ingest; rotated on re-pair |

Helpers in `app/db/secrets.py`: `save_mytracks_relay_api_key_to_db`, `load_mytracks_relay_api_key_from_db`, `mytracks_relay_api_key_stored_in_db` (mirror Tailwind / SMTP patterns).

---

## Implementation PR stack (domesti-bot)

| PR | Scope |
| --- | --- |
| **D1** | `presence_store.upsert_participant_fix`; relay-key verifier; `PresenceWebhookIn`; `POST /v1/webhooks/presence` + `POST /v1/webhooks/presence/test`; `PUT /v1/presence/{id}`; hermetic tests |
| **D2** | `app_secrets` relay key helpers; `pair_with_my_tracks()` client; `mytracks_settings` migration; `POST /v1/settings/my-tracks/pair` (generate + persist key); `GET …/pair-status`; `PATCH …/location-updates` |
| **D3** | Settings UI: pairing form (both HTTPS URLs), verify, emergency toggle, toasts |
| **D4** (later) | `RuleEvaluator` on live presence ingest only |

**my-tracks companions:** [#1087](https://github.com/the-hcma/my-tracks/pull/1087) (pair + config); add `participant_location_test_url` to pair payload; P3 live relay; P4 test button → **test URL only**.

---

## Testing strategy

| Layer | Coverage |
| --- | --- |
| Live webhook | Auth; known vs unknown participant; upsert fix; `503` when emergency off |
| Test webhook | Same validation; no persistence; works when emergency off live path |
| Pair | HTTPS URL validation; relay key generated + stored; my-tracks 200/4xx mapping; re-pair rotates key |
| Relay key auth | Webhook accepts DB key only; rejects env `DOMESTI_API_KEY`; `401` when not paired |
| Verify | Mock my-tracks test endpoint → test webhook → `204` |
| Emergency toggle | Local `503` + PATCH my-tracks mocked |
| Integration | Manual: pair on staging HTTPS hosts → verify → sync roster → my-tracks test → status shows fix |

---

## Success criteria

1. Operator pairs from domesti-bot using **HTTPS** URLs for both services; domesti-bot generates a relay secret, persists it encrypted, and registers **live + test** webhook URLs and that secret on my-tracks.
2. **Verify pairing** succeeds end-to-end via `…/presence/test` without mutating live rule state.
3. Live GPS fixes update `/v1/rules/participants/status` when emergency switch is on.
4. Emergency switch stops live ingest (`503`) and pauses my-tracks relay.
5. Participant and geofence data still flow only via **manual** sync pulls.

---

## References

- my-tracks: [docs/DOMESTI_BOT_INTEGRATION_PLAN.md](https://github.com/the-hcma/my-tracks/blob/main/docs/DOMESTI_BOT_INTEGRATION_PLAN.md)
- my-tracks PR: [feat: domesti-bot config model, pairing API, and admin panel #1087](https://github.com/the-hcma/my-tracks/pull/1087)
- domesti-bot rule engine (evaluator, rules UI): `docs/RULE_ENGINE_PLAN.md`
- Existing sync client: `app/mytracks_service.py`
- Existing settings routes: `app/api/mytracks_routes.py`
