# Vizio Wake-on-LAN — conclusion

This note records field validation for Vizio Wake-on-LAN (WoL) on the Kitchen TV and the decision to **remove WoL from domesti-bot**. The code path lived in `app/vizio_wol.py` and `VizioTvDevice.turn_on` (removed in the PR that supersedes this investigation).

## Kitchen TV (field unit)

| Field | Value |
| --- | --- |
| Label | Kitchen TV |
| Model | `V505M-K09` |
| Cached host | `192.168.86.201:7345` |
| MAC | `00:bd:3e:d5:f0:11` |
| domesti-bot host | `192.168.86.11` (same `/24`) |

## Re-verification (2026-06-18)

After the operator disabled **Eco Mode** and confirmed **Quick Start** power mode:

| Check | Result |
| --- | --- |
| Power mode API | Quick Start (Eco not active) |
| After REST `power_off` | TCP **7345 stayed OPEN** for 3+ minutes (Quick Start standby) |
| Closed-port WoL test | **Invalid** — port never closed, so the historical “send magic packet, poll 7345” probe could not run |
| REST `power_on` with Quick Start | **Works** without WoL |
| WoL fallback on `turn_on` | Added **~60s latency** with **no observed benefit** |

With Quick Start enabled, the TV keeps SmartCast reachable in standby. WoL was designed for the opposite case (port closed, TV asleep). That case did not apply on 2026-06-18, and when it did apply historically, WoL never helped.

## Eco Mode vs Quick Start

Upstream Vizio SmartCast documentation and integrations (pyvizio, Home Assistant, openHAB) state that **Wake-on-LAN does not work when the TV is in Eco Mode**; Quick Start (or equivalent) is typically required for remote power-on.

| Power mode | SmartCast port 7345 in standby | Remote power-on via REST | WoL |
| --- | --- | --- | --- |
| **Eco Mode** | Usually **closed**; TV may drop from ARP/SSDP | Unreachable until manual wake | Documented as **unsupported / ignored** |
| **Quick Start** | Often **stays open** in standby | **`power_on` REST works** | Unnecessary when port is already open |

The operator moved this set to Quick Start. domesti-bot should rely on REST when the API is reachable and fail fast when it is not — not block on a 60s WoL wait.

## Historical failure evidence (June 15–17, 2026)

When TCP 7345 **was** closed (Eco-style or deep sleep):

- **June 15 post-#294 redeploy:** port closed → WoL probe ran ~63s → `did not answer SmartCast after Wake-on-LAN; treating as off`. Discovery total **66.1s**.
- **Only WoL-related log line** in the log file for that period; no “woke successfully” record.
- **Pre-#294 bootstrap:** `SmartCast port closed` warning, discovery **~3s**, off tile — **no WoL sent**.
- **Local WoL probes (2026-06-15):** magic packets sent via multiple broadcast/unicast strategies; port 7345 remained closed for 30–45s waits; TV did not appear on LAN.

The `turn_on` WoL path (since #268) only ran after a failed `power_on` REST call. It **never succeeded** on this TV in production logs.

## Decision

**WoL support was removed from domesti-bot.** MAC-based identity, discovery cache, auth storage, and DHCP re-resolution are **unchanged** — normalized MAC remains the stable `device_id` for auto-discovered TVs (SQLite `vizio_known_tvs`, `vizio_auth:{mac}` secrets, UI routes, ARP IP lookup).

| Behavior | Before | After |
| --- | --- | --- |
| Stable TV identifier | Normalized **MAC** (`device_id`) | **Unchanged** |
| Discovery cache / auth keyed by MAC | Yes | **Unchanged** |
| ARP / SmartCast MAC resolution | Yes | **Unchanged** |
| Bootstrap unreachable TV | Register as **off** immediately | Unchanged |
| `turn_on` when SmartCast reachable | REST `power_on` | Unchanged |
| `turn_on` when SmartCast unreachable | WoL + 60s port poll + retry REST | **`VizioSmartCastConnectionError` propagates immediately** |
| Magic-packet WoL | `app.vizio_wol.send_wake_on_lan` | **Removed** (wake only; not used for identity) |
| MAC normalization helper | `app/vizio_wol.normalize_mac` | `app/vizio_mac.normalize_mac` |

### Operator guidance

1. **Use Quick Start** (Settings → System → Power Mode) so SmartCast stays reachable in standby and REST `power_on` works from domesti-bot.
2. **Avoid Eco Mode** if you need remote power-on; when Eco/deep sleep makes the TV unreachable, domesti-bot **`turn_on` fails fast** with a connection error — wake the TV manually (remote or physical power).
3. No WoL toggle or magic-packet tooling is shipped in this repo; re-enabling WoL would require new field proof that it works on your firmware/power mode.

## References

- Vizio SmartCast API notes (WoL unsupported in Eco Mode): [exiva/Vizio_SmartCast_API](https://github.com/exiva/Vizio_SmartCast_API/blob/master/README.md)
- Home Assistant Vizio integration (Eco Mode power-on limitation): [home-assistant.io/integrations/vizio](https://www.home-assistant.io/integrations/vizio/)
- Related PRs: #294 (bootstrap WoL), #295 (cap bootstrap wait / logging), follow-up removal of bootstrap WoL, this PR (remove `turn_on` WoL path)
