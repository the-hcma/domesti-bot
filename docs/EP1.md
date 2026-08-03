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
- Rule conditions: occupancy device-state and `ep1_reading_compare` (JSON-authored;
  see `automation-rules.json.example`)

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
   - **Bluetooth proxy** — prefer **No** (fewer Wi‑Fi issues)
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

### Settings Test

☰ → **Settings** → **EP1** → optional **Test host** → **Test**.

- Homey plaintext: leave PSK empty; provide a host (or rely on a cached /
  discovered endpoint).
- Encrypted firmware: enter the Noise PSK (and host if needed).

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
| Occupancy | `occupancy` | Binary sensor |
| Temperature | `temperature` / `temperature_sensor` | Sensor (°C) |
| Humidity | `humidity` / `humidity_sensor` | Sensor (%) |
| Illuminance | `illuminance` / `illuminance_sensor` | Sensor (lx) |

Other EP1 entities (mmWave knobs, PIR, LEDs, firmware update, …) are ignored by
the tile / header path today.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Skipped / zero sensors | Same L2 as the EP1? Try `discover-ep1` or `avahi-browse`. Cross-VLAN: set `EP1_HOSTS` to a unicast IP. |
| Connect fails with encryption errors | Homey should be plaintext — clear any stored PSK. Encrypted YAML needs the matching key. |
| Header empty | No EP1 in UI state yet (discovery/connect failed), or zero devices after MAC filter. |
| Blinking red, no Wi‑Fi SSID | Expected — use the USB flasher / ESPHome Web Wi‑Fi step, not a soft-AP. |
| IP changed after DHCP | Cache updates on successful reconnect; mDNS rediscover (`discover-ep1` / `refresh-discovery`) if stuck. |

## Related

- Root [README](../README.md) — feature list and env vars
- [`automation-rules.json.example`](../automation-rules.json.example) — EP1 rule examples
- Vendor docs: [EP1](https://docs.everythingsmart.io/s/products/doc/everything-presence-one-ep1-3R178yZSUP),
  [USB flasher](https://docs.everythingsmart.io/flash/everything-presence-one.html)
