# Manual browser verification

The Settings panels, the Content-Security-Policy and the admin-key prompt have only been exercised by unit and API tests. Run this checklist in a real browser after changes to `web/`, `app/api/app.py` security headers, the settings routes or the pairing flow, and record the result in the PR or issue. It takes about fifteen minutes.

## Setup

- Start the server in three configurations and repeat the section 3 cases for each: split keys (set `DOMESTI_API_KEY` and `DOMESTI_ADMIN_API_KEY` to two different values), a single key (only `DOMESTI_API_KEY`), and admin-only (only `DOMESTI_ADMIN_API_KEY`). Serve the page through whatever injects `<meta name="domesti-api-key">`, or set the control key in the tag with the browser tools for a local test.
- Run this against a disposable instance: a copy of the database file, its own `DOMESTI_BOT_CONFIG_FILE` and a throwaway My Tracks, not your live setup. Several steps overwrite or delete stored credentials, re-pair My Tracks, or change the Fernet key.
- Use a clean profile (no extensions), open the browser console and the network tab, and keep **Preserve log** on.
- Build the bundle first: `pnpm run build` in `web/`.

## 1. Content-Security-Policy

- [ ] Load `/` with the console open: no `Content-Security-Policy` violation messages.
- [ ] The Leaflet stylesheet from unpkg loads and the geofence map renders its controls.
- [ ] OpenStreetMap tiles load on the presence map and in the geofence editor.
- [ ] Device artwork (LAN images) loads on the tiles.
- [ ] `GET /static/compact-layout-prototype.html` is a 404.
- [ ] The response for `/` and `/static/index.html` carries `Content-Security-Policy`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: strict-origin-when-cross-origin`.
- [ ] Settings responses (`/v1/settings/...`) carry `Cache-Control: no-store`.
- [ ] Trying to embed the page in an `<iframe>` from another origin is blocked (`frame-ancestors 'none'`).

## 2. Write-only secret panels (Kasa, Tailwind, Vizio, EP1, SMTP, My Tracks)

For each panel:

- [ ] With nothing stored the field is empty, required (SMTP password: optional) and shows the empty-state placeholder.
- [ ] Save a value: the field clears and shows "Saved. Leave blank to keep current". Reload the page: the placeholder is still there and the value is not in the DOM (inspect the input, the network responses and `localStorage`/`sessionStorage`).
- [ ] Press **Test** with the field blank: it tests the stored credential and reports a result without sending the value back.
- [ ] Type a new value and save: the placeholder stays and the old value is replaced (Test now uses the new one).
- [ ] Clear/delete the stored value: the field returns to the empty-state placeholder.
- [ ] If the stored value cannot be decrypted: stop the server, start it with a different `DOMESTI_BOT_SECRETS_KEY` (disposable instance only), and the panel shows the unreadable-key note (the EP1 note stays visible after the device panels load) and offers re-entering the value. Do not save a replacement value while the changed key is active: it would overwrite the unreadable row under the new key, and restoring the original key cannot bring that row back. Afterwards restore the original key and restart before any normal use.
- [ ] The password reveal (eye) toggle only reveals text you typed, never a stored value.

## 3. Admin-key prompt (scoped API keys)

- [ ] **Split keys**: open Settings with only the control key in the meta tag. A prompt asks for the admin key; entering the right key loads the panels; the key is not in the DOM, `localStorage`, `sessionStorage` or cookies afterwards.
- [ ] Several panels request at once: only one prompt appears.
- [ ] A wrong admin key shows "That API key was not accepted"; Cancel closes the prompt, and for 30 seconds other requests do not re-prompt.
- [ ] Reload the page: the admin key is forgotten and Settings asks again.
- [ ] Ordinary actions (toggle a device, bulk off) keep working with the control key and never send the admin key (network tab: `X-Domesti-Api-Key` value on `/v1/ui/...` is the control key).
- [ ] **Single key**: with only `DOMESTI_API_KEY`, Settings loads with no prompt.
- [ ] **Admin-only**: with only `DOMESTI_ADMIN_API_KEY` and no meta tag, the first protected request prompts and the typed key is then used everywhere (including the dashboard).
- [ ] The Rules hub and Settings dialogs still detect the settings API when it answers 403 for the control key (they show the live panels, not mock data).

## 4. My Tracks pairing (relay protocol 2)

- [ ] Pair with a My Tracks that supports protocol 2: the panel says "Relay protocol 2: separate keys per direction", no key is shown anywhere.
- [ ] Pair again: "The previous key is still accepted until ..." appears for about a minute, then disappears; **Revoke previous key** ends it at once after a confirmation.
- [ ] Pair with an old My Tracks: the panel says "Relay protocol 1: one shared key", and with **Require relay protocol 2** ticked the pairing is refused before anything changes.
- [ ] Block My Tracks after the activation request (stop it or cut the network at that moment): the pairing shows "Activation sent but not confirmed" with **Check activation**; once My Tracks is back, Check activation promotes it and the status becomes protocol 2.
- [ ] While an activation is unconfirmed, pairing again is refused with the "Check activation, or reset the pairing" message, and **Reset** clears it.
- [ ] Send a test location from My Tracks (Admin panel): it arrives, with and without the pairing in the grace window.
- [ ] My Tracks Admin panel shows "Version 2" / "Version 1" and never a key; `GET /api/admin/domesti-bot/reveal-api-key/` is a 404.

## 5. Record the result

Paste this into the PR or issue:

| Area | Browser and version | Result | Notes |
| --- | --- | --- | --- |
| CSP |  |  |  |
| Write-only panels |  |  |  |
| Admin-key prompt |  |  |  |
| My Tracks pairing |  |  |  |
