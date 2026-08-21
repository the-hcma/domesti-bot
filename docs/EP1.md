# Everything Presence One (EP1)

Operator guide for bringing an [Everything Presence One](https://docs.everythingsmart.io/s/products/doc/everything-presence-one-ep1-3R178yZSUP)
onto the LAN **without Home Assistant**, then letting `domesti-bot` discover it
and show live climate / light readings in the dashboard header.

For the tracking issue and field notes from the first Homey bring-up, see
[#568](https://github.com/the-hcma/domesti-bot/issues/568).

## What you get

- Occupancy tiles (read-only) under the **EP1** family in `/v1/ui/state`
- Temperature (°C / °F), humidity, and illuminance in the **dashboard header**
  strip when at least one EP1 is connected (no mock / `data-mock` filler)
- Header metrics are **green** while the sensor is responding and **yellow**
  when stale (`EP1_HEADER_STALE_AFTER_S` = 3x `EP1_HEADER_EXPECTED_REFRESH_PERIOD_S`,
  default 15s). Liveness follows subscription activity (`last_heard_at`), not
  “climate values changed”
- **Person / ghost** glyph immediately right of the orange bulk-off button
  (occupied / clear). Occupied paints with `--accent` (green); clear uses a
  light silver on the dark canvas and a darker gray in light/clear mode so
  the ghost stays visible on the white main panel. Glyphs are stroke
  outlines (ghost keeps eye dots); live header metrics are bold. In light
  appearance, live readings use a darker green than `--accent` so they
  contrast on the white panel. Desktop hover on the climate/light readings
  or the occupancy glyph uses the same identity block as tiles (label, then
  MAC and IP). Multiple EP1s: occupied if any *responding* sensor is
  occupied, else clear if any responding sensor is clear, else hidden
  (including when all EP1s are stale)
- Rule conditions: occupancy device-state and `ep1_reading_compare` (JSON-authored;
  see `automation-rules.json.example`)

**Reading-kind wakes:** lux / temperature / humidity entity pushes re-evaluate
`device_state` rules that subscribe via `ep1_reading_compare` (optionally gated
by a top-level `local_time_window` or daylight eligibility). Full subscribe
contract, wake routing, and non-EP1 out-of-scope notes:
[`docs/RULE_ENGINE_PLAN.md`](RULE_ENGINE_PLAN.md) — *Design: reading-kind
subscribe contract (#670 / #672)*.

EP1 is **2.4 GHz Wi‑Fi only**. There is no soft-AP hotspot when the unit blinks
red after power-on — that means “ready for USB setup,” not “join my SSID.”

## Flash firmware (USB, no Home Assistant)

### Prerequisites

- Desktop **Chrome or Edge** (Web Serial)
- USB cable to the EP1
- **2.4 GHz** Wi‑Fi SSID + password
- Same LAN / VLAN as the host running `domesti-bot` (for mDNS discovery)
- Sticker / purchase date for hardware revision + mmWave variant

### Install

1. Open the [Everything Presence One firmware installer](https://docs.everythingsmart.io/flash/everything-presence-one.html).
2. Select:
   - **Hardware revision** — from the sticker (Rev 1.6 / 1.5 / 1.3–1.4)
   - **mmWave sensor** — SEN0609 (~after Mar 2024) or SEN0395 (earlier)
   - **Bluetooth proxy** — select **Yes** only for BLE phone proximity work
     ([#526](https://github.com/the-hcma/domesti-bot/issues/526) /
     [#528](https://github.com/the-hcma/domesti-bot/issues/528)); otherwise
     prefer **No** (fewer Wi‑Fi issues)
   - **CO2 module** — only if installed
   - **Firmware channel** — **Stable**
   - **Platform** — **Homey** is the recommended path for domesti-bot
     (unencrypted ESPHome native API on port **6053**). Home Assistant platform
     also works without running HA; encryption is usually unset until something
     adopts the device.
3. Connect USB → allow serial access → **Install Firmware**.
4. On the post-install **Configure Wi‑Fi** screen, join the **2.4 GHz** network.

If Wi‑Fi was skipped: reconnect in normal run mode and use
[ESPHome Web Tools](https://web.esphome.io/) → **Configure Wi‑Fi**, or
[improv-wifi.com](https://improv-wifi.com/).

Vendor alternative: [USB setup guide](https://docs.everythingsmart.io/s/products/doc/alternative-setup-guide-for-ep1-in-home-assistant-6gw3IQiDO5).

## Find the device on the LAN

Stock firmware advertises ESPHome mDNS:

| Item | Value |
| --- | --- |
| Service type | `_esphomelib._tcp.local.` |
| Hostname pattern | `everything-presence-one-<suffix>.local` |
| API port | **6053** |

Example on a Linux host with Avahi:

```bash
avahi-browse -atrk | rg -i 'everything|presence|esphomelib'
avahi-resolve -n everything-presence-one-<suffix>.local
```

TXT records typically include `project_name=Everything Smart Technology.Everything Presence One`,
`friendly_name=…`, `mac=…`, and firmware `version=…`.

**Homey / pre-adoption firmware is plaintext** — no Noise encryption key.
Do not invent a PSK. Optional later: flash custom ESPHome YAML with
`api.encryption.key` and store that key under Settings → **EP1**.

## Point domesti-bot at the sensor

### Automatic (preferred)

On the same LAN as the EP1, start or restart `domesti-bot-server` / the CLI.
When the discovery cache has no EP1 rows and no explicit hosts are set,
bootstrap browses `_esphomelib._tcp` for Everything Presence One nodes
(unless `--no-ep1-zeroconf`).

Successful connect writes `ep1_known_devices` in the discovery SQLite DB
(`~/.cache/rule-engine/device_discovery.sqlite` by default). Later startups
are cache-first (reconnect by host; no mDNS until rediscover / empty cache).

REPL helpers:

```text
discover-ep1              # mDNS browse; prints hosts and readings when possible
read-ep1 [name|mac|id]    # live-read occupancy / temp / humidity / lux (all sensors if no arg)
refresh-discovery         # includes EP1 rediscover with other backends
```

### Explicit host (optional)

Use when mDNS is blocked (cross-VLAN multicast) or you want a pinned address:

| Mechanism | Example |
| --- | --- |
| Env | `EP1_HOSTS=192.168.1.50` or `everything-presence-one-….local` (comma-separated; `:port` optional) |
| CLI | `--ep1-host 192.168.1.50` (repeatable) |
| Disable mDNS | `--no-ep1-zeroconf` |

Optional Noise PSK (encrypted firmware only):

| Mechanism | Example |
| --- | --- |
| Env | `EP1_NOISE_PSK=…` |
| CLI | `--ep1-noise-psk …` |
| UI | ☰ → **Settings** → **EP1** |

### Settings Test, calibration, occupancy, and Bluetooth proxy

☰ → **Settings** → **EP1** → **Target device** (shared dropdown for Test, BLE,
offsets, and occupancy).

- Homey plaintext: leave PSK empty; select a target device and tap **Test**.
- Encrypted firmware: enter the Noise PSK (and select a target).
- **Bluetooth proxy** — enable/disable the on-device `bluetooth_proxy` select
  (`Disabled` / `Enabled`). **Enable and Test** turns the proxy on if needed,
  listens ~20s for raw BLE advertisements, and lists MAC + RSSI (API-key
  protected Settings). Empty results usually mean the proxy is on but the
  scanner still delivers no ads ([#590](https://github.com/the-hcma/domesti-bot/issues/590)).
- **Humidity / illuminance / temperature offsets** load for the selected target;
  **Apply offsets** writes changed values to that EP1 only (ESPHome `number`
  entities; persisted on-device).
- **Occupancy tuning** (SEN0609 mmWave): min/max distance, trigger distance,
  trigger/sustain sensitivity, on/off latency. **Apply occupancy tuning** writes
  changed numbers, then automatically presses firmware **Set Distance** (after
  min/max distance changes) and **Set Sensitivity** (after sensitivity changes).
  Latency and trigger distance apply via the number's own `set_action`.
  SEN0395 exposes a subset (single sensitivity, latencies, and a single
  `mmwave_distance` maximum that applies without Set Distance); missing knobs
  show as unavailable.

domesti-bot **mirrors** the combined `occupancy` binary for tiles and rules —
it does not invent presence. False “occupied when empty” is almost always
placement, power, or mmWave tuning (see Troubleshooting).

## Verify

1. Startup / REPL shows **Everything Presence One** ready with a non-zero sensor count.
2. `GET /v1/ui/state` includes family `ep1` with `occupancy_readings`
   (`temperature_c`, `humidity_pct`, `illuminance_lx`).
3. Dashboard header shows live metrics (no `data-mock="true"`).
4. Occupancy tiles appear under the EP1 family section.

## Entity mapping

domesti-bot maps ESPHome entities by `object_id` / name aliases:

| Role | Typical `object_id` | Type |
| --- | --- | --- |
| Occupancy | `occupancy` | Binary sensor (tiles / rules) |
| Temperature | `temperature` / `temperature_sensor` | Sensor (°C) |
| Humidity | `humidity` / `humidity_sensor` | Sensor (%) |
| Illuminance | `illuminance` / `illuminance_sensor` | Sensor (lx) |
| Temp / humidity / lux offsets | `temperature_offset`, `humidity_offset`, `illuminance_offset` | Number (Settings → Calibration) |
| mmWave min / max distance | `mmwave_minimum_distance`, `mmwave_max_distance` | Number (Settings → Occupancy; needs Set Distance) |
| mmWave trigger distance | `mmwave_trigger_distance` | Number (Settings → Occupancy) |
| mmWave sustain / trigger sensitivity | `mmwave_sustain_sensitivity`, `mmwave_trigger_sensitivity` | Number (Settings → Occupancy; needs Set Sensitivity) |
| mmWave on / off latency | `mmwave_on_latency`, `mmwave_off_latency` | Number (Settings → Occupancy) |
| Set Distance / Set Sensitivity | `set_distance`, `set_sensitivity` | Button (pressed automatically after Apply) |

Other EP1 entities (raw `mmwave` / `pir` binaries, LEDs, firmware update, threshold
factor, factory-reset mmWave, …) stay unused by tiles / header / Settings v1.
Factory-reset mmWave remains available via ESPHome / Home Assistant if a module
is stuck.

## BLE phone proximity

Room-scale BLE proximity is tracked separately from occupancy / climate:

| Issue | Role |
| --- | --- |
| [#519](https://github.com/the-hcma/domesti-bot/issues/519) | Live EP1 entity + BLE advertisement spike |
| [#526](https://github.com/the-hcma/domesti-bot/issues/526) | Product: observe beacons → map to `user_id` → rules / UI |
| [#528](https://github.com/the-hcma/domesti-bot/issues/528) | Phone transmitters **without** Home Assistant Companion |

My Tracks GPS remains **home-scale** presence (`user` / `location` /
geofences). EP1 BLE is **room-scale**. Prefer `ble_proximity` /
`ble_distance_m` / conditions such as `users_within_ble_distance` — do not
overload GPS nomenclature.

### Phone transmitters (no Home Assistant required)

[#526](https://github.com/the-hcma/domesti-bot/issues/526) matches any stable
**iBeacon identity** (`uuid` ± `major` / `minor`) to a roster `user_id`. The
phone only needs to **advertise**; it does not need Companion or a Home
Assistant install. Research ranking for always-on presence TX
([#528](https://github.com/the-hcma/domesti-bot/issues/528)):

| Rank | App | Notes |
| --- | --- | --- |
| 1 | **[Beacon Simulator](https://play.google.com/store/apps/details?id=net.alea.beaconsimulator)** ([source](https://github.com/vhiribarren/beacon-simulator-android), GPL-3) | Best “phone as beacon” UX; background TX; config library. **Caveat:** iBeacon packet format is stripped from the OSS tree (Apple license); the Play Store build includes iBeacon. FOSS builds still do AltBeacon / Eddystone. |
| 2 | **[Beaconer](https://github.com/pccr10001/beaconer)** (GPL-3 + [Play](https://play.google.com/store/apps/details?id=li.power.app.beaconer)) | Best **pure OSS iBeacon** transmitter: UUID / major / minor / TX power / auto-start. Small/young — promising, not battle-tested. |
| 3 | **[HA Companion](https://companion.home-assistant.io/) [BLE Transmitter](https://companion.home-assistant.io/docs/core/sensors/)** | Proven in HA ecosystems; extras (TX only on home Wi‑Fi SSID, notification commands). Optional if already installed; overkill if you never run HA. |

**Default recommendation for operators (no HA):**

- Prefer **Beacon Simulator (Play Store)** when you want the mature set-and-forget
  UX and are fine using the proprietary iBeacon packet in that build.
- Prefer **Beaconer** when you want a fully OSS iBeacon stack end-to-end.
- Keep **HA Companion BLE Transmitter** as an optional emitter only — never as
  a hard requirement for #526.

Skip Spectre / Beacon Toy / DIY Beacon Library TX unless experimenting; they
are not calm always-on presence beamers.

#### Practical phone caveats (any transmitter)

- Device must support **BLE peripheral / advertising**.
- Modern Android needs a **foreground service** (persistent notification) for
  continuous TX.
- Battery cost is real — gate TX when possible (home SSID, charging, etc.).
- The phone app only **emits**; a BLE-enabled EP1 (or another scanner) must
  **observe**.
- iOS transmitters are out of scope for [#528](https://github.com/the-hcma/domesti-bot/issues/528)
  (separate research if needed).

#### Suggested operator setup (when #526 ships)

1. Flash EP1 with **Bluetooth proxy = Yes** (see Flash firmware above).
2. On each phone, install Beacon Simulator or Beaconer; configure a fixed
   UUID / major / minor per person (document the values somewhere private —
   not in tracked fixtures).
3. Start continuous TX (accept the foreground notification).
4. Confirm ads with
   `uv run scripts/internal/probe-ep1-ble --host <ep1-ip-or-hostname> --duration 30`
   (pass your EP1 address — the script’s default host is a maintainer test bench).
   while standing near the EP1.
5. Map `{uuid, major, minor}` → roster `user_id` in Settings / JSON (product
   work in #526).

### EP1 observation paths

Stock **BLE** firmware enables `esp32_ble_tracker` + `bluetooth_proxy` aimed at
Home Assistant’s Bluetooth integration. It does **not** ship per-user
`ble_presence` / distance entities.

Without HA, domesti-bot’s realistic paths are:

| Path | How | HA required? |
| --- | --- | --- |
| **A (preferred)** | `APIClient.subscribe_bluetooth_le_raw_advertisements` → parse iBeacon / AltBeacon locally | No |
| **B** | Custom ESPHome `ble_presence` / `ble_rssi` entities matching UUID/major/minor | No (custom firmware) |
| **C** | Stock proxy → HA Bluetooth (+ Bermuda) → scrape HA | Yes |

```text
Phone (Beaconer / Beacon Simulator / Companion TX)
    → EP1 BLE firmware (tracker + proxy)
        → domesti-bot (raw ads or entities → ble_proximity → user_id → rules)
```

Both Path A and Path B require an EP1 image flashed with **Bluetooth proxy
enabled**. A Homey image without that component cannot supply BLE data.

**Reflash is destructive.** Installing a different firmware image can replace
on-device firmware and wipe persisted settings (Wi‑Fi, calibration offsets,
occupancy tuning, optional Noise key). Before reflashing: note or back up any
Settings → EP1 values you care about, then confirm explicitly that you intend to
reflash this unit. Only then install the BLE-enabled variant (see Flash firmware
above) and rejoin Wi‑Fi. Do not enable or change the proxy configuration from a
diagnostic probe.

### Wire-format sketch for #526 (not locked)

Until live ads are captured on [#519](https://github.com/the-hcma/domesti-bot/issues/519),
treat this as a design sketch only:

- Store beacon → user bindings as
  `{ "user_id": "…", "ibeacon_uuid": "…", "major": N, "minor": M }`
  (major/minor optional when the UUID alone is unique per person).
- Prefer Settings / encrypted store over committing identities in rules JSON.
- Rules reference `user_id` only; never put raw UUIDs in condition prose.
- Start with **presence / RSSI band** rules if meter estimates are noisy; add
  `ble_distance_m` only after a calibrated RSSI→meters model exists.

### Live native-API spike (2026-08-05)

The plaintext Homey EP1 at `192.168.86.214:6053` (`28:05:a5:28:c8:48`) accepted
an unauthenticated native-API connection. `list_entities_services` returned 41
entities, including the `bluetooth_proxy` select, but no `ble_presence`,
`ble_rssi`, or phone-distance entity. A 20-second raw-advertisement
subscription succeeded without Home Assistant connected, but delivered **zero**
advertisement records, so there are no address/RSSI/address-type/data samples
to report.

This proves that the current image advertises a Bluetooth Proxy control and
accepts the raw BLE RPC; it does **not** prove that its scanner is enabled or
that it can receive advertisements. Treat the current result as “BLE data
unavailable pending firmware/proxy verification,” rather than proof that the
hardware is No-BLE. Re-run the non-mutating maintainer probe after flashing or
enabling a known BLE-capable image **with a phone TX running nearby**:

```bash
uv run scripts/internal/probe-ep1-ble --host <ep1-ip-or-hostname> --duration 30
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Skipped / zero sensors | Same L2 as the EP1? Try `discover-ep1` or `avahi-browse`. Cross-VLAN: set `EP1_HOSTS` to a unicast IP. |
| Connect fails with encryption errors | Homey should be plaintext — clear any stored PSK. Encrypted YAML needs the matching key. |
| Header empty | No EP1 in UI state yet (discovery/connect failed), or zero devices after MAC filter. |
| Blinking red, no Wi‑Fi SSID | Expected — use the USB flasher / ESPHome Web Wi‑Fi step, not a soft-AP. |
| BLE probe accepts the RPC but prints zero advertisements | Confirm Bluetooth proxy firmware + scanner enabled; start a phone iBeacon TX (Beacon Simulator / Beaconer) near the EP1; then rerun `uv run scripts/internal/probe-ep1-ble --host <ep1-ip-or-hostname> --duration 30`. |
| IP changed after DHCP | Cache updates on successful reconnect; mDNS rediscover (`discover-ep1` / `refresh-discovery`) if stuck. |
| Occupied when the room is empty | Not a UI bug — occupancy mirrors the ESPHome binary. Checklist: (1) placement (fans, HVAC, curtains, plants, vibrating mounts, mirrors/glass, motion through thin walls); (2) quality USB supply (≥1A); (3) lower **max distance** to the room size; (4) lower trigger/sustain sensitivity; (5) raise on-latency slightly; (6) power-cycle / factory-reset mmWave via ESPHome if still stuck. Vendor: [How to Tune Your EP1 Sensor](https://docs.everythingsmart.io/s/products/doc/how-to-tune-your-ep1-sensor-eJwL48QXTH). Adjust knobs under Settings → EP1 → Occupancy tuning. |

## Related

- Root [README](../README.md) — feature list and env vars
- [`automation-rules.json.example`](../automation-rules.json.example) — EP1 rule examples
- Vendor docs: [EP1](https://docs.everythingsmart.io/s/products/doc/everything-presence-one-ep1-3R178yZSUP),
  [USB flasher](https://docs.everythingsmart.io/flash/everything-presence-one.html),
  [Tune guide](https://docs.everythingsmart.io/s/products/doc/how-to-tune-your-ep1-sensor-eJwL48QXTH)
- Tracking: occupancy tuning [#582](https://github.com/the-hcma/domesti-bot/issues/582);
  BLE spike [#519](https://github.com/the-hcma/domesti-bot/issues/519);
  BLE proximity product [#526](https://github.com/the-hcma/domesti-bot/issues/526);
  Android TX research [#528](https://github.com/the-hcma/domesti-bot/issues/528)
