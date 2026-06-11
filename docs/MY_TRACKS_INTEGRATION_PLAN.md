# Plan: my-tracks integration (domesti-bot companion)

This document is the **domesti-bot** side of integrating with [my-tracks](https://github.com/the-hcma/my-tracks). The my-tracks companion plan lives at `docs/DOMESTI_BOT_INTEGRATION_PLAN.md` in that repository.

**Rule evaluation, geofence definitions, and device actions stay in domesti-bot.** my-tracks remains the location ingest and map service. After pairing, my-tracks **pushes** live location updates to domesti-bot; roster and geofence **definitions** are still pulled manually by domesti-bot (no my-tracks push webhooks for those).

**Status:** **domesti-bot integration is complete** for the operator workflow shipped in PRs [#209](https://github.com/the-hcma/domesti-bot/pull/209)–[#216](https://github.com/the-hcma/domesti-bot/pull/216): relay-key webhooks, pairing APIs + Settings UI (pair/re-pair, retention, relay-key reveal, reset), manual roster/geofence sync, live user map with legend + polling, and `scripts/internal/verify-mytracks-pairing`. **Deferred (not integration blockers):** verify-roundtrip and emergency-toggle **buttons** in Settings (APIs exist; use the verify script today), `RuleEvaluator` on live ingest (see `docs/RULE_ENGINE_PLAN.md`), and my-tracks production cutover for live relay (my-tracks repo).

---

## Responsibilities split

| Concern | Owner | Mechanism |
| --- | --- | --- |
| OwnTracks ingest, map, friends | my-tracks | Existing MQTT/HTTP → SQLite |
| User roster (automation) | my-tracks (source of truth) | **Manual pull** by domesti-bot (`POST /v1/rules/users/sync`) |
| Geofence definitions (automation) | domesti-bot | **Manual pull** (`POST /v1/rules/geofences/sync`) from my-tracks export APIs |
| Live location updates for rules | my-tracks → domesti-bot | **Automatic push** after pairing (`POST` to domesti-bot webhook URLs) |
| Rule evaluation & device actions | domesti-bot | `RuleEvaluator` (future PRs; see `docs/RULE_ENGINE_PLAN.md`) |

---

## HTTPS, public URLs, and secrets

Both services are reached at **HTTPS public endpoints** in production (e.g. `https://tracks.example.com`, `https://domesti.example.com`). LAN `http://` addresses are dev-only.

Pairing does **not** require deploy-time env vars for URLs or the my-tracks relay secret. The operator supplies URLs in the pairing UI; domesti-bot generates and persists the relay secret.

| Item | Who provides it | When |
| --- | --- | --- |
| **My Tracks base URL** | Operator | Pairing form (HTTPS) |
| **domesti-bot public base URL** | domesti-bot derives | From each pairing request (`X-Forwarded-*` / ASGI base URL); operator does not edit a separate public URL field |
| **Relay API key** | domesti-bot generates | At pair (or re-pair); stored encrypted in SQLite; sent once to my-tracks |
| **User location update URL** | domesti-bot derives | `{domesti_public_base_url}/v1/webhooks/location_update` — registered on my-tracks at pair |
| **User location test URL** | domesti-bot derives | `{domesti_public_base_url}/v1/webhooks/location_update/test` — registered on my-tracks at pair |

Listen bind (`--listen-all`, port 8003, reverse proxy, TLS termination) is orthogonal — webhooks and pairing always use the **public** HTTPS origin from the pairing form, not the internal bind address.

### Relay secret (my-tracks webhooks only)

domesti-bot is the **source of truth** for the relay secret. my-tracks only receives it at pair time.

1. On **Pair** / **Re-pair**, domesti-bot generates a new key (`secrets.token_urlsafe(32)`).
2. domesti-bot stores it encrypted in `app_secrets` under e.g. `mytracks_relay_api_key` (Fernet via `app/db/secrets.py`, same pattern as Tailwind token).
3. domesti-bot POSTs the plaintext key once to my-tracks `POST /api/admin/domesti-bot/pair/`.
4. my-tracks stores its copy encrypted in `DomestiBotConfig`.
5. Incoming `POST /v1/webhooks/location_update` and `…/location_update/test` validate `X-Domesti-Api-Key` against the **decrypted DB key**, not `DOMESTI_API_KEY`.

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
| User roster sync | `POST /v1/rules/users/sync` + `GET …/sync-status` |
| Geofence sync | `POST /v1/rules/geofences/sync` + `GET …/sync-status` |
| Latest location per user (status UI) | `rule_user_last_location` — upserted on live webhook + user sync |
| Location history (per user) | `rule_user_location_history` — append on live ingest; pruned by retention policy |
| User status for UI | `GET /v1/rules/users/status` |
| Admin session client to my-tracks | `app/mytracks_service.py` (login + export JSON) |
| Live + test location webhooks | `POST /v1/webhooks/location_update`, `POST /v1/webhooks/location_update/test` |
| Debug location ingest | `PUT /v1/location_update/{user_id}` |
| Pairing APIs | `POST /v1/settings/my-tracks/pair`, `GET /v1/settings/my-tracks/pair-status` |
| Emergency switch (API) | `PATCH /v1/settings/my-tracks/location-updates` |
| Location-history retention (API) | `PATCH /v1/settings/my-tracks/location-history-retention` |
| Pairing UI (Settings → My Tracks) | `web/src/my-tracks-pairing-panel.ts` — pair/re-pair, retention, relay-key reveal, reset |
| User presence map (Automations) | `web/src/presence-map.ts` — legend, device colors, 5s status polling |
| Operator verify script | `scripts/internal/verify-mytracks-pairing` |

**Deferred:** verify-roundtrip button and emergency-toggle button in Settings (APIs: `PATCH /v1/settings/my-tracks/location-updates`, `last_verify_*` fields on pair-status); `RuleEvaluator` on live ingest (`docs/RULE_ENGINE_PLAN.md`).

---

## Pairing flow (domesti-bot → my-tracks)

Pairing is **initiated from domesti-bot**. The relay secret is **never** typed into my-tracks; domesti-bot generates it, persists it, and pushes it once pairing succeeds.

```mermaid
sequenceDiagram
  participant Op as Operator
  participant Bot as domesti-bot
  participant MT as my-tracks

  Op->>Bot: Settings → My Tracks: My Tracks HTTPS URL + admin user
  Op->>Bot: Pair (admin password prompt)
  Bot->>Bot: Generate relay_api_key; encrypt → app_secrets
  Bot->>Bot: Build location-update + test webhook URLs from public URL
  Bot->>MT: POST /api/admin/domesti-bot/pair/ (admin session)
  Note over Bot,MT: api_key, user_location_update_url,<br/>user_location_test_url, domesti_base_url
  MT->>MT: Store DomestiBotConfig, location_updates_enabled=true
  MT-->>Bot: 200 paired
  Bot->>Bot: Persist paired_at + URLs in mytracks_settings
  Op->>Bot: Optional: Sync users + geofences
  Note over MT,Bot: Later: location updates → POST …/location_update<br/>Tests → POST …/location_update/test
```

### domesti-bot pairing endpoint (implemented)

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
  "location_history_retention": {
    "max_age_hours": 24,
    "min_keep_count": 20,
    "unlimited": false
  },
  "username": "admin",
  "password": "<admin password, not stored>"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `domain` | yes | Operator-supplied **HTTPS** my-tracks public URL |
| `domesti_public_base_url` | no | Omitted from pairing body — domesti-bot derives the public HTTPS origin from the pairing HTTP request |
| `location_history_retention` | no | Defaults: 24h window ∪ 20 most recent fixes per user; see [Location history retention](#location-history-retention) |
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
  "user_location_update_url": "https://domesti.example.com/v1/webhooks/location_update",
  "user_location_test_url": "https://domesti.example.com/v1/webhooks/location_update/test"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `api_key` | yes | Fresh relay secret; domesti-bot persists encrypted before send; my-tracks stores encrypted; never returned in API responses |
| `domesti_base_url` | yes | From pairing form |
| `user_location_update_url` | yes | Live GPS relay target |
| `user_location_test_url` | yes | Synthetic / verify traffic only (see below) |

**Responses:**

- `200` — pair succeeded; body includes `paired_at`, echoed public URLs; `relay_key_configured: true` (never the key itself).
- `400` / `422` — validation (bad URL, missing Fernet key, my-tracks rejected body).
- `502` — my-tracks unreachable or pair endpoint error.

**Side effects:** upsert My Tracks settings (`domain`, `username`, public URL, webhook URLs, location-history retention), encrypt/store relay key in `app_secrets`, set `paired_at`, clear `last_pair_error`. Pairing attempts log at `[mytracks] pairing starting|complete|failed` (INFO/WARNING).

### my-tracks companion change

PR [the-hcma/my-tracks#1087](https://github.com/the-hcma/my-tracks/pull/1087) adds `user_location_update_url` only. Before cutover, my-tracks must also accept and persist **`user_location_test_url`** (same validation: absolute HTTPS URL). my-tracks **Test location update** (P4) and domesti-bot **Verify pairing** must POST to the **test** URL, never the live relay URL.

---

## Webhook endpoints (domesti-bot)

Both routes require `X-Domesti-Api-Key` matching the **relay secret** from `app_secrets` (decrypted at request time). They use a dedicated verifier — **not** the env-based `_verify_api_key` used for operator UI routes. They must **not** depend on device discovery (`_device_state`).

### Live location relay

```
POST /v1/webhooks/location_update
```

**Body** (`LocationUpdateWebhookIn`):

```json
{
  "user_id": "kristen",
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
| `204` | Location accepted and stored |
| `401` | Not paired, missing header, or relay key mismatch |
| `404` | Unknown `user_id` (roster not synced) |
| `422` | Bad coordinates or timestamp |
| `503` | **Emergency switch** — domesti-bot is dropping location updates (see below) |

**Side effects:**

- Upsert `rule_user_last_location` (latest snapshot for status UI / geofence checks).
- Append one row to `rule_user_location_history`, then prune per [retention policy](#location-history-retention).
- Log `[location] stored location for …` at INFO; prune logs `[location] pruned N history row(s) for …`.
- Future: `RuleEvaluator.request_evaluation(reason="location_update")`.

Honour `timestamp` when present (ISO-8601 UTC preferred); readings older than the cached latest are ignored (no history append).

### Test location relay

```
POST /v1/webhooks/location_update/test
```

Same request body and auth as live relay. Differences:

| Aspect | Live `/location_update` | Test `/location_update/test` |
| --- | --- | --- |
| Trigger | my-tracks on each saved location update | my-tracks P4 test button; domesti-bot verify roundtrip |
| Respects emergency switch | yes (`503`) | **no** — verify must still work when live ingest is paused |
| Persists location | yes (latest + history) | **no** — validate auth + schema; return `204` without writing (accepts any `user_id` for connectivity checks); logs `test webhook accepted for … (discarded)` |
| Evaluator | future: schedule | never |

This keeps production rule state clean while operators confirm connectivity.

### Debug ingest (optional, same PR as live relay)

```
PUT /v1/location_update/{user_id}
```

Same fields as webhook body for curl/manual debugging; same responses as live relay.

---

## Pairing configuration tab (Settings → My Tracks)

The My Tracks settings tab ships **connection settings** (domain + default admin username) and a **pairing section** (`web/src/my-tracks-pairing-panel.ts`).

### Connection (implemented)

- Fields: **My Tracks URL** (HTTPS), **default admin username**, Save / Clear.
- Sync prompts (Automations hub) reuse the saved domain + username.

### Pairing section (shipped — D3)

- **Location history retention** controls (see below) — editable before pair and via **Save retention** when paired.
- **Pair** / **Re-pair** → password prompt → `POST /v1/settings/my-tracks/pair` (includes retention in body).
- **Relay API key** reveal (read-only after pair).
- **Reset** clears pairing metadata and relay key.
- Status line: `Paired at …`, `Last pairing failed: …`, or **Not paired**.
- Public domesti-bot URL is derived server-side from the pairing request (not a separate editable field).
- **Deferred UI:** verify-roundtrip button, emergency toggle (use `scripts/internal/verify-mytracks-pairing` and `PATCH …/location-updates` until buttons land).

### Pairing status API (implemented)

```
GET /v1/settings/my-tracks/pair-status
```

```json
{
  "paired_at": "2026-06-10T14:00:00Z",
  "domain": "https://tracks.example.com",
  "username": "admin",
  "domesti_public_base_url": "https://domesti.example.com",
  "user_location_update_url": "https://domesti.example.com/v1/webhooks/location_update",
  "user_location_test_url": "https://domesti.example.com/v1/webhooks/location_update/test",
  "relay_key_configured": true,
  "location_history_retention": {
    "max_age_hours": 24,
    "min_keep_count": 20,
    "unlimited": false
  },
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

**Goal:** Confirm my-tracks can reach domesti-bot with the stored relay secret, without writing a live location update or firing rules.

**Flow (domesti-bot orchestrates):**

1. Authenticate to my-tracks (admin session, saved domain + username + password prompt).
2. Call my-tracks `POST /api/admin/domesti-bot/test-location-update/` with optional `user_id` (defaults to first roster member after sync).
3. my-tracks POSTs a **synthetic** payload to the stored **`user_location_test_url`** (`…/location_update/test`), not the live URL.
4. domesti-bot test handler validates and returns `204`.
5. my-tracks returns success/failure to domesti-bot; domesti-bot surfaces result in UI (toast + `last_verify_at` / `last_verify_ok`).

If step 2 is unavailable (older my-tracks), fallback: domesti-bot `POST`s synthetic body to its own test URL internally and reports “local handler OK” only — document as degraded verify.

---

## Emergency switch: drop location updates

**UI:** Toggle on pairing configuration tab — e.g. **“Accept location updates”** (on by default).

Two layers (operator sees one toggle; implementation applies both):

| Layer | Mechanism | Effect |
| --- | --- | --- |
| **domesti-bot (local)** | `location_updates_accepted` in SQLite | Live `POST /v1/webhooks/location_update` returns **`503`** with `Retry-After`; locations not stored; evaluator not scheduled |
| **my-tracks (upstream)** | `PATCH /api/admin/domesti-bot/config/` with `{ "location_updates_enabled": false }` | my-tracks stops sending live relays (ingest unaffected) |

Turning the switch **off** sets both. Turning **on** re-enables both (PATCH `true`). Test URL and **Verify pairing** remain available when live ingest is paused.

```
PATCH /v1/settings/my-tracks/location-updates
{ "accepted": false }
```

Requires paired state; returns updated `pair-status` shape.

---

## Location history retention

Live webhook ingest **persists history**, not just the latest location. Each accepted `POST /v1/webhooks/location_update` appends to `rule_user_location_history` and updates `rule_user_last_location` (denormalized latest for `/v1/rules/users/status` and geofence containment).

### Default policy

Keep a location reading when **either**:

1. `received_at` is within the last **24 hours**, or
2. the location is among the **20 most recent** for that user.

This union keeps *more* data than either rule alone (the operator-facing help text: “last day **or** last 20, whichever is larger”).

### Configuration

Stored on `mytracks_settings` (set at pair time; updatable without re-pair):

| Column | Purpose |
| --- | --- |
| `location_history_unlimited` | `1` = never prune history |
| `location_history_max_age_s` | Age window (default `86400`) |
| `location_history_min_keep_count` | Minimum recent rows to keep (default `20`) |

**API:**

```
PATCH /v1/settings/my-tracks/location-history-retention
{
  "max_age_hours": 24,
  "min_keep_count": 20,
  "unlimited": false
}
```

Changing retention runs an immediate prune across all users. `unlimited: true` skips pruning entirely.

**UI:** Settings → My Tracks → Pairing → “Location history per user” fieldset (checkbox **Keep all location history**, hours, min count).

**Module:** `app/location_history_retention.py` (`retained_history_row_ids`, defaults); prune in `app/presence_store.py`.

Test webhook (`…/location_update/test`) and emergency-off live path do **not** append history.

---

## Manual sync (unchanged)

After pairing, operators still run **Sync from my-tracks** in the Automations hub (users + geofences). my-tracks does not push roster or geofence changes.

User sync must run **before** live webhooks succeed (`404` until `user_id` exists in domesti-bot).

---

## Persistence (domesti-bot SQLite)

### `mytracks_settings` (additive migration)

| Column | Purpose |
| --- | --- |
| `paired_at` | Last successful pair (epoch) |
| `last_pair_error` | Optional operator-visible failure |
| `domesti_public_base_url` | Public HTTPS origin from pairing form |
| `user_location_update_url` | Live webhook URL sent at pair |
| `user_location_test_url` | Test webhook URL sent at pair |
| `location_updates_accepted` | Emergency switch (default `true`) |
| `location_history_unlimited` | Never prune location history when `1` |
| `location_history_max_age_s` | Retention age window (default `86400`) |
| `location_history_min_keep_count` | Minimum recent fixes to keep (default `20`) |
| `last_verify_at` / `last_verify_ok` | Last verify roundtrip result |

### `rule_user_location_history`

| Column | Purpose |
| --- | --- |
| `id` | Autoincrement primary key |
| `user_id` | Roster member |
| `lat`, `lon`, `accuracy_m`, `received_at`, `source` | Fix payload |
| `updated_at` | Insert time (epoch) |

Pruned after each append (and when retention settings change) per [Location history retention](#location-history-retention). `rule_user_last_location` remains the fast “latest only” read path.

### `app_secrets` (encrypted)

| Key | Purpose |
| --- | --- |
| `mytracks_relay_api_key` | Relay secret generated at pair; validated on webhook ingest; rotated on re-pair |

Helpers in `app/db/secrets.py`: `save_mytracks_relay_api_key_to_db`, `load_mytracks_relay_api_key_from_db`, `mytracks_relay_api_key_stored_in_db` (mirror Tailwind / SMTP patterns).

---

## Implementation PR stack (domesti-bot)

| PR | Scope | Status |
| --- | --- | --- |
| **D1** | Relay-key verifier; `LocationUpdateWebhookIn`; live + test webhooks; `PUT /v1/location_update/{id}`; `rule_user_location_history` + retention prune; hermetic tests | **Done** ([#209](https://github.com/the-hcma/domesti-bot/pull/209)) |
| **D2** | `app_secrets` relay key; `pair_with_my_tracks()`; pairing APIs; `PATCH …/location-updates`; `PATCH …/location-history-retention`; pairing attempt logging | **Done** ([#209](https://github.com/the-hcma/domesti-bot/pull/209)) |
| **D3** | Settings UI: pair/re-pair, retention, relay-key reveal, reset; Automations user map legend + live polling | **Done** ([#209](https://github.com/the-hcma/domesti-bot/pull/209)–[#216](https://github.com/the-hcma/domesti-bot/pull/216)) |
| **D3b** (optional) | Verify-roundtrip + emergency-toggle buttons in Settings | Deferred (APIs + verify script exist) |
| **D4** (later) | File-backed `RuleEvaluator` on live `POST /v1/webhooks/location_update` (rules in `automation-rules.json`, no rule SQLite yet) | In progress — bundle in `automation-rules.json.example`; see **Phase 2a** in `docs/RULE_ENGINE_PLAN.md` |

**my-tracks companions:** [#1087](https://github.com/the-hcma/my-tracks/pull/1087) (pair + config); add `user_location_test_url` to pair payload; P3 live relay; P4 test button → **test URL only**.

---

## Testing strategy

| Layer | Coverage |
| --- | --- |
| Live webhook | Auth; known vs unknown user; latest + history append; retention prune; `503` when emergency off |
| Test webhook | Same validation; no persistence; works when emergency off live path |
| Location history | Default 24h ∪ 20 readings; unlimited mode; `PATCH …/location-history-retention` |
| Pair | HTTPS URL validation; relay key generated + stored; my-tracks 200/4xx mapping; pairing logs |
| Relay key auth | Webhook accepts DB key only; rejects env `DOMESTI_API_KEY`; `401` when not paired |
| Verify script | `scripts/internal/verify-mytracks-pairing` — direct my-tracks pair + webhook smoke |
| Emergency toggle | Local `503` + PATCH my-tracks mocked (API only; Settings toggle deferred) |
| Integration | Manual: deploy [#209](https://github.com/the-hcma/domesti-bot/pull/209) → pair via Settings or verify script → sync roster → my-tracks test → status shows location |

---

## Success criteria

1. Operator pairs from domesti-bot using **HTTPS** URLs for both services; domesti-bot generates a relay secret, persists it encrypted, and registers **live + test** webhook URLs and that secret on my-tracks.
2. **Verify pairing** succeeds end-to-end via `…/location_update/test` without mutating live rule state (today: `scripts/internal/verify-mytracks-pairing`; Settings button deferred).
3. Live location updates refresh `/v1/rules/users/status` when emergency switch is on; history rows accumulate subject to retention.
4. Emergency switch stops live ingest (`503`) and pauses my-tracks relay.
5. User roster and geofence data still flow only via **manual** sync pulls.
6. Operators can tune or disable location-history pruning from the pairing UI (default: 24h ∪ 20 readings).

---

## References

- my-tracks: [docs/DOMESTI_BOT_INTEGRATION_PLAN.md](https://github.com/the-hcma/my-tracks/blob/main/docs/DOMESTI_BOT_INTEGRATION_PLAN.md)
- my-tracks PR: [feat: domesti-bot config model, pairing API, and admin panel #1087](https://github.com/the-hcma/my-tracks/pull/1087)
- domesti-bot rule engine (evaluator, rules UI): `docs/RULE_ENGINE_PLAN.md`
- Existing sync client: `app/mytracks_service.py`
- Existing settings routes: `app/api/mytracks_routes.py`
