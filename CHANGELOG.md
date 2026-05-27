# Changelog

## [1.0.0](https://github.com/the-hcma/domesti-bot/compare/v0.1.4...v1.0.0) (2026-05-27)


### ⚠ BREAKING CHANGES

* resume Sonos zones from stream favorites ([#143](https://github.com/the-hcma/domesti-bot/issues/143))

### Features

* resume Sonos zones from domesti-secrets stream favorites ([#141](https://github.com/the-hcma/domesti-bot/issues/141)) ([140dbcf](https://github.com/the-hcma/domesti-bot/commit/140dbcfdb963e0241e6695e72c2e06db54cc6ef6))
* resume Sonos zones from stream favorites ([#143](https://github.com/the-hcma/domesti-bot/issues/143)) ([67690f2](https://github.com/the-hcma/domesti-bot/commit/67690f2856f6ecf84aac0b6d2eacc585ded9bf28))

## [0.1.4](https://github.com/the-hcma/domesti-bot/compare/v0.1.3...v0.1.4) (2026-05-21)


### Bug Fixes

* **ci:** publish release-please checks correctly ([#128](https://github.com/the-hcma/domesti-bot/issues/128)) ([40f551d](https://github.com/the-hcma/domesti-bot/commit/40f551d3fbadf0235657b449c1dc4a0b323a55a2))
* **ci:** publish required checks on release-please PRs ([#127](https://github.com/the-hcma/domesti-bot/issues/127)) ([7ccb593](https://github.com/the-hcma/domesti-bot/commit/7ccb593148b04cd3a68d5183f6f369f6fc895207))
* **ci:** run release-PR checks on reopen ([#126](https://github.com/the-hcma/domesti-bot/issues/126)) ([8d1d869](https://github.com/the-hcma/domesti-bot/commit/8d1d869e08fef46fad90068393dab341006224cd))
* **ci:** run required checks on Release Please PRs ([#125](https://github.com/the-hcma/domesti-bot/issues/125)) ([28fe3e6](https://github.com/the-hcma/domesti-bot/commit/28fe3e64d0255ce7df9be56eb99d611cba5803f3))
* **on-deploy:** check web/node_modules before skipping deploy ([#124](https://github.com/the-hcma/domesti-bot/issues/124)) ([7d1d06b](https://github.com/the-hcma/domesti-bot/commit/7d1d06b01c0f8796e376239e3e0641b684586c79))
* **on-deploy:** stop false rebuilds from deploy-input mtimes ([#122](https://github.com/the-hcma/domesti-bot/issues/122)) ([0ea6382](https://github.com/the-hcma/domesti-bot/commit/0ea6382cdefa990029c132e35e6d6402d7ec4211))

## [0.1.3](https://github.com/the-hcma/domesti-bot/compare/v0.1.2...v0.1.3) (2026-05-21)


### Documentation

* require squash merge for Release Please changelog ([#120](https://github.com/the-hcma/domesti-bot/issues/120)) ([d74a3fd](https://github.com/the-hcma/domesti-bot/commit/d74a3fd64dd8eb02c5c6de4469f831ac169d0d9e))

## [0.1.2](https://github.com/the-hcma/domesti-bot/compare/v0.1.1...v0.1.2) (2026-05-21)


### Documentation

* document CVE exceptions for 10-day release age policy ([#113](https://github.com/the-hcma/domesti-bot/pull/113))
* shrink README mobile UI screenshot ([#108](https://github.com/the-hcma/domesti-bot/pull/108))
* simplify release-age policy to cutover-only grandfather ([#115](https://github.com/the-hcma/domesti-bot/pull/115))

## [0.1.1](https://github.com/the-hcma/domesti-bot/compare/v0.1.0...v0.1.1) (2026-05-19)

Documentation and dependency maintenance since the first PyPI release.

### Documentation

* PyPI badge, published-release notes, and updated publish checklists ([#103](https://github.com/the-hcma/domesti-bot/pull/103))
* Mobile UI screenshot in README (`docs/images/domesti-bot-mobile-ui.jpeg`) ([#105](https://github.com/the-hcma/domesti-bot/pull/105))

### Chore

* Dependency updates ([#104](https://github.com/the-hcma/domesti-bot/pull/104))

## 0.1.0 (2026-05-19)

First public release. Install with `pipx install domesti-bot` (see [RELEASING.md](docs/RELEASING.md)).

### Features

#### Core API & server

* Deferred device discovery, `/health`, and background state polling ([#15](https://github.com/the-hcma/domesti-bot/pull/15))
* `GET /v1/ui/state` and REST control for Kasa, Tailwind, and Sonos ([#5](https://github.com/the-hcma/domesti-bot/pull/5), [#6](https://github.com/the-hcma/domesti-bot/pull/6), [#7](https://github.com/the-hcma/domesti-bot/pull/7), [#29](https://github.com/the-hcma/domesti-bot/pull/29))
* `GET /v1/meta`, build metadata, and web brand mark ([#42](https://github.com/the-hcma/domesti-bot/pull/42))
* Per-device `exclude_from_global` preferences in SQLite ([#4](https://github.com/the-hcma/domesti-bot/pull/4))
* SQLAlchemy persistence and encrypted Tailwind token settings ([#56](https://github.com/the-hcma/domesti-bot/pull/56))
* `setup-secrets` REPL command ([#57](https://github.com/the-hcma/domesti-bot/pull/57))
* GoTailwind settings UI polish and hot-reload after token save ([#58](https://github.com/the-hcma/domesti-bot/pull/58))
* Kasa KLAP auth hints and `kasa-creds` REPL command ([#19](https://github.com/the-hcma/domesti-bot/pull/19), [#20](https://github.com/the-hcma/domesti-bot/pull/20))
* `--listen-all` for LAN test binds ([#34](https://github.com/the-hcma/domesti-bot/pull/34))
* Auto-open browser when running interactively on loopback ([#16](https://github.com/the-hcma/domesti-bot/pull/16))
* Google Cast temporarily disabled pending on/off fix ([#26](https://github.com/the-hcma/domesti-bot/pull/26))
* `on-deploy` rebuild when web bundle, venv, or inputs drift ([#70](https://github.com/the-hcma/domesti-bot/pull/70))
* PyPI packaging, console scripts, Release Please, and pipx docs ([#97](https://github.com/the-hcma/domesti-bot/pull/97))

#### Web UI

* TypeScript tile dashboard (pnpm + esbuild) ([#3](https://github.com/the-hcma/domesti-bot/pull/3))
* Unified tile toggles, optimistic updates, and connectivity framing ([#11](https://github.com/the-hcma/domesti-bot/pull/11), [#12](https://github.com/the-hcma/domesti-bot/pull/12), [#14](https://github.com/the-hcma/domesti-bot/pull/14), [#17](https://github.com/the-hcma/domesti-bot/pull/17), [#21](https://github.com/the-hcma/domesti-bot/pull/21), [#22](https://github.com/the-hcma/domesti-bot/pull/22))
* Bulk-off (per-family and global), exclude checkboxes, orange bulk buttons, and action toasts ([#24](https://github.com/the-hcma/domesti-bot/pull/24), [#25](https://github.com/the-hcma/domesti-bot/pull/25), [#27](https://github.com/the-hcma/domesti-bot/pull/27), [#77](https://github.com/the-hcma/domesti-bot/pull/77), [#96](https://github.com/the-hcma/domesti-bot/pull/96))
* Discovery spinner instead of a 503 error banner ([#28](https://github.com/the-hcma/domesti-bot/pull/28))
* Sonos zones in the tile UI and 409 action-error toasts ([#29](https://github.com/the-hcma/domesti-bot/pull/29), [#31](https://github.com/the-hcma/domesti-bot/pull/31))
* Per-family header icons ([#32](https://github.com/the-hcma/domesti-bot/pull/32))
* PWA manifest, service worker, install hint, and theme toggle ([#47](https://github.com/the-hcma/domesti-bot/pull/47), [#51](https://github.com/the-hcma/domesti-bot/pull/51), [#52](https://github.com/the-hcma/domesti-bot/pull/52), [#53](https://github.com/the-hcma/domesti-bot/pull/53))
* Compact mobile tile layout, static SVG icons, and typography polish ([#61](https://github.com/the-hcma/domesti-bot/pull/61)–[#67](https://github.com/the-hcma/domesti-bot/pull/67), [#71](https://github.com/the-hcma/domesti-bot/pull/71)–[#73](https://github.com/the-hcma/domesti-bot/pull/73), [#76](https://github.com/the-hcma/domesti-bot/pull/76), [#81](https://github.com/the-hcma/domesti-bot/pull/81)–[#87](https://github.com/the-hcma/domesti-bot/pull/87))
* About dialog, empty-discovery mascot, and brand tooltip ([#50](https://github.com/the-hcma/domesti-bot/pull/50), [#64](https://github.com/the-hcma/domesti-bot/pull/64), [#72](https://github.com/the-hcma/domesti-bot/pull/72), [#76](https://github.com/the-hcma/domesti-bot/pull/76))

### Bug Fixes

* Kasa discovery dedup by host ([#18](https://github.com/the-hcma/domesti-bot/pull/18))
* Tailwind open/close idempotent for global all-off ([#23](https://github.com/the-hcma/domesti-bot/pull/23))
* Chromecast session teardown on turn-off ([#2](https://github.com/the-hcma/domesti-bot/pull/2))
* Compact/mobile layout, labels, icons, and scroll preservation ([#62](https://github.com/the-hcma/domesti-bot/pull/62), [#63](https://github.com/the-hcma/domesti-bot/pull/63), [#68](https://github.com/the-hcma/domesti-bot/pull/68), [#74](https://github.com/the-hcma/domesti-bot/pull/74), [#84](https://github.com/the-hcma/domesti-bot/pull/84), [#88](https://github.com/the-hcma/domesti-bot/pull/88), [#89](https://github.com/the-hcma/domesti-bot/pull/89))
* PWA cache bump and blank-page boot hint ([#69](https://github.com/the-hcma/domesti-bot/pull/69), [#49](https://github.com/the-hcma/domesti-bot/pull/49))
* Tailwind token dialog UX ([#59](https://github.com/the-hcma/domesti-bot/pull/59), [#60](https://github.com/the-hcma/domesti-bot/pull/60))
* Refresh device labels from hardware on cache reconnect ([#91](https://github.com/the-hcma/domesti-bot/pull/91))
* About dialog fonts on tablet ([#101](https://github.com/the-hcma/domesti-bot/pull/101))
* Global bulk-off when tiles still looked active ([#102](https://github.com/the-hcma/domesti-bot/pull/102))

### Chore & documentation

* PR-only workflow and pre-push hook ([#1](https://github.com/the-hcma/domesti-bot/pull/1))
* Rename user-facing branding to domesti-bot ([#8](https://github.com/the-hcma/domesti-bot/pull/8))
* `http.HTTPStatus`, context managers, and code-style conventions ([#9](https://github.com/the-hcma/domesti-bot/pull/9))
* Parallel hermetic CI, browser job split, dev dependency group ([#40](https://github.com/the-hcma/domesti-bot/pull/40), [#75](https://github.com/the-hcma/domesti-bot/pull/75))
* Systemd user unit (LAN bind, health check) ([#41](https://github.com/the-hcma/domesti-bot/pull/41), [#46](https://github.com/the-hcma/domesti-bot/pull/46), [#48](https://github.com/the-hcma/domesti-bot/pull/48))
* CODEOWNERS, Dependabot, and Graphite merge queue docs ([#33](https://github.com/the-hcma/domesti-bot/pull/33), [#83](https://github.com/the-hcma/domesti-bot/pull/83))
* MIT license, README, and roadmap PLAN ([#39](https://github.com/the-hcma/domesti-bot/pull/39), [#54](https://github.com/the-hcma/domesti-bot/pull/54), [#78](https://github.com/the-hcma/domesti-bot/pull/78))
* Quieter `/v1/ui/state` access logs ([#30](https://github.com/the-hcma/domesti-bot/pull/30))
* Dependency updates ([#44](https://github.com/the-hcma/domesti-bot/pull/44), [#55](https://github.com/the-hcma/domesti-bot/pull/55), [#79](https://github.com/the-hcma/domesti-bot/pull/79), [#94](https://github.com/the-hcma/domesti-bot/pull/94), [#99](https://github.com/the-hcma/domesti-bot/pull/99))
* Repository-helpers `on-deploy-deps` integration ([#92](https://github.com/the-hcma/domesti-bot/pull/92), [#93](https://github.com/the-hcma/domesti-bot/pull/93))
