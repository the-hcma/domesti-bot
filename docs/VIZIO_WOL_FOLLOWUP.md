# Vizio Wake-on-LAN — follow-up (deferred)

Companion to **PR #295** (`fix: cap Vizio bootstrap WoL wait and improve discovery logging`). That PR caps bootstrap discovery blocking and improves logging; **this note tracks WoL validation we still owe** before treating bootstrap WoL as reliable in production.

## Background

PR **#294** added a one-shot WoL + SmartCast wait during Vizio bootstrap (`_wake_and_probe_tv` in `_offline_tv`) so eco/standby TVs could establish an initial on/off baseline instead of staying **unknown**. After merge, discovery blocked ~66s when the TV never answered, and logs showed no evidence WoL had ever succeeded on this install.

## Kitchen TV (field unit)

| Field | Value |
| --- | --- |
| Label | Kitchen TV |
| Model | `V505M-K09` |
| Cached host | `192.168.86.201:7345` |
| MAC | `00:bd:3e:d5:f0:11` |
| domesti-bot host | `192.168.86.11` (same `/24`) |

## What the logs show

- **June 15 post-#294 redeploy:** TCP 7345 closed → WoL probe ran ~63s → `did not answer SmartCast after Wake-on-LAN; treating as off`. Discovery total **66.1s**.
- **Only WoL-related log line in the entire log file** is from that cycle; there is no prior “woke successfully” record.
- **Pre-#294 bootstrap** on the same TV: `SmartCast port closed` warning, discovery **~3s**, off tile — **no WoL sent**.
- **June 12 ~14:20:** TV was reachable (fast discovery, toggle attempted). That aligns with the TV being **already on** during pairing/use, not WoL from deep sleep.

## Local WoL probes (2026-06-15, TV asleep)

Baseline: ping fails, SSDP finds **0** Vizio hosts, ARP has **no** row for the cached MAC (`192.168.86.201` → `<incomplete>`), port **7345 closed**.

| Method | Broadcast / bind | Result after wait |
| --- | --- | --- |
| `app.vizio_wol.send_wake_on_lan` | `255.255.255.255` | Port closed (45s) |
| Same | `192.168.86.255` | Port closed (45s) |
| Same | unicast `192.168.86.201` | Port closed (45s) |
| Raw magic packet | bound to `192.168.86.11` → subnet / global / unicast | Port closed (30s each) |
| System `wakeonlan` | default | Port closed (30s) |
| `etherwake` | `enp1s0` | Not run (requires root) |

Packets were sent; the TV did not bring up SmartCast or appear on the LAN.

## Likely root cause (unconfirmed on-set)

Upstream Vizio SmartCast documentation and integrations (pyvizio, Home Assistant, openHAB) state:

> **Wake-on-LAN does not work when the TV is in Eco Mode** (Quick Start / normal power modes may be required).

Our field symptoms match eco/standby: no L2 presence, no SSDP, port 7345 down, magic packets ignored.

The `turn_on` WoL path (since #268) only runs after a failed `power_on` REST call; it has **never been proven** on this TV from logs.

## PR #295 scope (what we ship now)

- Cap bootstrap WoL wait at **12s** (`_WOL_BOOTSTRAP_WAIT_DEADLINE_S`); keep **60s** for user-initiated `turn_on`.
- Demote `/v1/ui/state` discovery **503** access lines to **TRACE**.
- Add `[startup]` per-backend discovery timing logs and Vizio bootstrap probe lines.

We **do not** claim WoL works from eco sleep until the checklist below passes.

## Follow-up checklist (later work)

### On the TV (operator)

1. **Power mode:** Settings → System → Power Mode — note current value. If **Eco Mode**, try **Quick Start** (or equivalent) and enable **Wake-on-LAN** if the menu exposes it.
2. **Baseline with TV awake:** Power on with the remote; confirm SmartCast answers on `192.168.86.201:7345` (or updated DHCP IP).
3. **Soft standby test:** With Quick Start enabled, turn **off** with the remote (standby, not long-press full power-off). From domesti-bot host, send WoL and poll 7345 for up to 60s.
4. **Eco standby test:** Repeat step 3 after eco-style off; record whether WoL is ignored (expected per docs).

### In code (after field validation)

- [ ] Add `scripts/verify_vizio_wol` (or similar) — MAC, host, broadcast strategies, port poll — for repeatable LAN checks without starting the full server.
- [ ] Prefer **subnet broadcast** (`192.168.86.255`) when the outbound interface is known; keep global broadcast as fallback.
- [ ] If eco mode cannot WoL: **skip bootstrap WoL** when we can detect eco / unreachable deep sleep, or gate behind config, so discovery never blocks on a known-no-op path.
- [ ] Log WoL send + strategy at INFO (bootstrap already improved in #295); log **success** when 7345 opens after WoL.
- [ ] Document required TV settings in operator docs (power mode, Quick Start, WoL toggle).
- [ ] Optional: integration test marked `@pytest.mark.integration` with env vars for MAC/host when hardware is available.

## Success criteria

1. From domesti-bot host, WoL opens **7345** within 60s after **remote standby** (not full eco), at least once reliably.
2. Bootstrap path: unreachable cached TV → WoL (if enabled) → REST baseline **or** fast off tile without multi-minute discovery block.
3. Operator docs state which TV power modes are supported.

## References

- Vizio SmartCast API notes (WoL unsupported in Eco Mode): [exiva/Vizio_SmartCast_API](https://github.com/exiva/Vizio_SmartCast_API/blob/master/README.md)
- Home Assistant Vizio integration (Eco Mode power-on limitation): [home-assistant.io/integrations/vizio](https://www.home-assistant.io/integrations/vizio/)
- Code: `app/vizio_wol.py`, `VizioTvDevice.turn_on`, `VizioDeviceManager._wake_and_probe_tv`
