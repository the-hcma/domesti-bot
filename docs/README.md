# domesti-bot documentation

Operator and contributor guides for this repository. For day-to-day device control and setup, start with the root **[README](../README.md)**.

| Document | Audience | Contents |
| --- | --- | --- |
| [EP1.md](EP1.md) | Operators | Everything Presence One USB bring-up (no Home Assistant), Homey plaintext API, mDNS discovery, Settings / CLI, BLE phone TX notes |
| [RULE_ENGINE_PLAN.md](RULE_ENGINE_PLAN.md) | Operators + contributors | File-backed automations, triggers, conditions, Phase 2 roadmap |
| [MY_TRACKS_INTEGRATION_PLAN.md](MY_TRACKS_INTEGRATION_PLAN.md) | Operators | Pairing, webhooks, roster/geofence sync with [my-tracks](https://github.com/the-hcma/my-tracks) |
| [PLAN.md](PLAN.md) | Contributors | Remaining UI/sync work (WebSocket push, etc.) |
| [RELEASING.md](RELEASING.md) | Maintainers | PyPI publish workflow |
| [STACKING.md](STACKING.md) | Contributors | gh-stack stacked PRs and GitHub merge queue |
| [AGENTS.md](AGENTS.md) | Contributors | Canonical dev standards (also at repo root via symlink) |

**Operator quick paths**

- **Tiles / bulk off** — start `./scripts/domesti-bot-server`, open the landing page.
- **EP1 sensors** — USB flash Homey firmware, same LAN as the server; see [EP1.md](EP1.md).
- **Automations** — desktop ☰ → **Automations**; rules live in `automation-rules.json` (see `automation-rules.json.example`).
- **My Tracks** — desktop ☰ → **Settings** → My Tracks (pair, sync users/geofences).
- **Secrets** — `domesti-bot.config.json`, Tailwind token, optional EP1 Noise PSK, SMTP under Automations → Mail.
