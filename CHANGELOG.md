# Changelog

## [1.14.0](https://github.com/the-hcma/domesti-bot/compare/v1.13.0...v1.14.0) (2026-07-07)


### Features

* add daylight astronomical rule condition ([#421](https://github.com/the-hcma/domesti-bot/issues/421)) ([ea43e55](https://github.com/the-hcma/domesti-bot/commit/ea43e55184383793452857ebd4902be2f3fff919))
* human-friendly ui-action log messages ([#420](https://github.com/the-hcma/domesti-bot/issues/420)) ([1bdeef4](https://github.com/the-hcma/domesti-bot/commit/1bdeef4408de656e02f07dc0fba0d26d666217d0))
* log UI device actions and tighten offline detection ([#414](https://github.com/the-hcma/domesti-bot/issues/414)) ([7f2de37](https://github.com/the-hcma/domesti-bot/commit/7f2de37258a2cf35e3a5209312a729cf5533f597))
* poll devices concurrently within each state watcher family ([#416](https://github.com/the-hcma/domesti-bot/issues/416)) ([f238c48](https://github.com/the-hcma/domesti-bot/commit/f238c48095a38dd85c1366d927042224fa0f1595))


### Bug Fixes

* improve KLAP recovery and keep Kasa tiles during rediscover (closes [#410](https://github.com/the-hcma/domesti-bot/issues/410)) ([#411](https://github.com/the-hcma/domesti-bot/issues/411)) ([dbd226d](https://github.com/the-hcma/domesti-bot/commit/dbd226d6664b5707af20d3c261fbc4132b4378a9))
* propagate shutdown through watchers and manager teardown ([#413](https://github.com/the-hcma/domesti-bot/issues/413)) ([3516351](https://github.com/the-hcma/domesti-bot/commit/351635147911e5fb43db303d378b811d56976921))
* suppress stale health probes that falsely mark UI offline (closes [#407](https://github.com/the-hcma/domesti-bot/issues/407)) ([#408](https://github.com/the-hcma/domesti-bot/issues/408)) ([497d9ee](https://github.com/the-hcma/domesti-bot/commit/497d9ee1fefdb720d730b12711fbeac25ea80378))

## [1.13.0](https://github.com/the-hcma/domesti-bot/compare/v1.12.0...v1.13.0) (2026-07-06)


### Features

* Kasa KLAP credentials in Settings and encrypted CLI ([#397](https://github.com/the-hcma/domesti-bot/issues/397)) ([3526b46](https://github.com/the-hcma/domesti-bot/commit/3526b46fef1debd4193f38a0fa97ef896c3f1651))
* reveal stored Kasa password in Settings (closes [#400](https://github.com/the-hcma/domesti-bot/issues/400)) ([#405](https://github.com/the-hcma/domesti-bot/issues/405)) ([d6f35a2](https://github.com/the-hcma/domesti-bot/commit/d6f35a2eaa261c4c286d41b7f54fbe6372daf15e))
* Test button on Settings auth cards ([#402](https://github.com/the-hcma/domesti-bot/issues/402)) ([7f42636](https://github.com/the-hcma/domesti-bot/commit/7f42636dccbf19514281a379297dda5785f7725d))

## [1.12.0](https://github.com/the-hcma/domesti-bot/compare/v1.11.0...v1.12.0) (2026-07-03)


### Features

* arm dwell rules on time-window eligibility ([#394](https://github.com/the-hcma/domesti-bot/issues/394)) ([876c9b7](https://github.com/the-hcma/domesti-bot/commit/876c9b77eb3b46238ef1bff46dc2f714f36424a8))
* device-state rule trigger (closes [#388](https://github.com/the-hcma/domesti-bot/issues/388)) ([#389](https://github.com/the-hcma/domesti-bot/issues/389)) ([c05e3b1](https://github.com/the-hcma/domesti-bot/commit/c05e3b161155912728c9baf88f43cd654d860990))
* dwell_satisfied trigger with curated dwell watches ([#391](https://github.com/the-hcma/domesti-bot/issues/391)) ([4d8eaf1](https://github.com/the-hcma/domesti-bot/commit/4d8eaf154c905a5efd1a5a26cdc8b6aac40a50cd))


### Bug Fixes

* consolidate SQLite schema sync and fix bootstrap race ([#386](https://github.com/the-hcma/domesti-bot/issues/386)) ([69eb142](https://github.com/the-hcma/domesti-bot/commit/69eb142cd6aee22837474bed7f169a372ddf1d9b))
* skip Vizio power keypress when already on or off ([#395](https://github.com/the-hcma/domesti-bot/issues/395)) ([9160e59](https://github.com/the-hcma/domesti-bot/commit/9160e596809cb8e258e95a6504952a7ecf01c7aa))


### Documentation

* spell out public-then-private lexicographic sort rule ([#384](https://github.com/the-hcma/domesti-bot/issues/384)) ([89ed697](https://github.com/the-hcma/domesti-bot/commit/89ed697bb9024ed38c3a498e2aeff067dc75d750))

## [1.11.0](https://github.com/the-hcma/domesti-bot/compare/v1.10.0...v1.11.0) (2026-07-02)


### Features

* evaluate evening-interior rule at window arm and on arrival ([#379](https://github.com/the-hcma/domesti-bot/issues/379)) ([#380](https://github.com/the-hcma/domesti-bot/issues/380)) ([940c278](https://github.com/the-hcma/domesti-bot/commit/940c278d157e91937246ac70ce31280f3f74d076))

## [1.10.0](https://github.com/the-hcma/domesti-bot/compare/v1.9.0...v1.10.0) (2026-07-01)


### Features

* evening interior lights on geofence enter with daily cap ([#375](https://github.com/the-hcma/domesti-bot/issues/375)) ([9d24028](https://github.com/the-hcma/domesti-bot/commit/9d24028ec484cf697c832d536663a4c42fc8a9fe))
* proactive location monitoring — stale watchdog + geofence approach mode ([#363](https://github.com/the-hcma/domesti-bot/issues/363)) ([073642b](https://github.com/the-hcma/domesti-bot/commit/073642be1a0d96b350ad510f28eac5421f6e92db))
* separate fix_at from reported_at for presence locations ([#371](https://github.com/the-hcma/domesti-bot/issues/371)) ([727bfa8](https://github.com/the-hcma/domesti-bot/commit/727bfa8c479034057b2c5ca0115cd663dea82933))
* windowed astronomical repeat schedule for evening interior lights ([#364](https://github.com/the-hcma/domesti-bot/issues/364)) ([c8ee721](https://github.com/the-hcma/domesti-bot/commit/c8ee7211049ba4184d2f387a6f7f9b92b297a5b9))


### Bug Fixes

* log &lt;not applicable&gt; for missing location-request context ([#367](https://github.com/the-hcma/domesti-bot/issues/367)) ([607d552](https://github.com/the-hcma/domesti-bot/commit/607d55210f1f7abab8235bb6b787d6d20aff7c35))
* run stale location watchdog every 30 minutes ([#366](https://github.com/the-hcma/domesti-bot/issues/366)) ([39edd54](https://github.com/the-hcma/domesti-bot/commit/39edd54b52cfd8ffa36b29a7819f4f391d2ae781))
* schedule rule evaluation from webhook worker threads ([#373](https://github.com/the-hcma/domesti-bot/issues/373)) ([a9e649c](https://github.com/the-hcma/domesti-bot/commit/a9e649c3ce4579f6a46b0cfe9e3eb79164e481e4))

## [1.9.0](https://github.com/the-hcma/domesti-bot/compare/v1.8.0...v1.9.0) (2026-06-29)


### Features

* on-demand location requests via my-tracks for geofence accuracy ([#361](https://github.com/the-hcma/domesti-bot/issues/361)) ([e0353d9](https://github.com/the-hcma/domesti-bot/commit/e0353d94284d1f7539595a18715a703f27bec609))
* simplify rule email device action summary ([#359](https://github.com/the-hcma/domesti-bot/issues/359)) ([4d96d71](https://github.com/the-hcma/domesti-bot/commit/4d96d71658ea8ac679b55bbb813473632fe996dc))

## [1.8.0](https://github.com/the-hcma/domesti-bot/compare/v1.7.0...v1.8.0) (2026-06-28)


### Features

* improve rule notification emails with device state and deep links ([#354](https://github.com/the-hcma/domesti-bot/issues/354)) ([7e6a586](https://github.com/the-hcma/domesti-bot/commit/7e6a58636763ae5fe66c5ddfd33ab4f1b82ac9cc))
* ingest location metadata, home WiFi roster, and BSSID presence ([#351](https://github.com/the-hcma/domesti-bot/issues/351)) ([564ed64](https://github.com/the-hcma/domesti-bot/commit/564ed640bc7f5ea8ef712f77aef46a7cf1c48425))
* schedule astronomical rules via daily materialized cron ([#358](https://github.com/the-hcma/domesti-bot/issues/358)) ([dc3751e](https://github.com/the-hcma/domesti-bot/commit/dc3751e86fecdf842286d48956b5137d6a0e218d))
* trust dwell timers with 10-minute location walkback ([#350](https://github.com/the-hcma/domesti-bot/issues/350)) ([430478d](https://github.com/the-hcma/domesti-bot/commit/430478d2ea71d1aeaf9f8dd14b68b7a45ac33567))


### Bug Fixes

* **ci:** key CI concurrency on head SHA ([#346](https://github.com/the-hcma/domesti-bot/issues/346)) ([2ada203](https://github.com/the-hcma/domesti-bot/commit/2ada203650e15ae48921b5a8f61d4a8278193efb))
* one-shot scheduled dwell rules per geofence presence episode ([#353](https://github.com/the-hcma/domesti-bot/issues/353)) ([51f3ba5](https://github.com/the-hcma/domesti-bot/commit/51f3ba5cf639f86882a8c26b58887010aafc6692))
* pin Kasa cache and rule email states after device actions ([#357](https://github.com/the-hcma/domesti-bot/issues/357)) ([a22483f](https://github.com/the-hcma/domesti-bot/commit/a22483f3f1dc249e5700859bd47a8e9d750d854e))

## [1.7.0](https://github.com/the-hcma/domesti-bot/compare/v1.6.0...v1.7.0) (2026-06-24)


### Features

* add devices_any_off rule condition ([#336](https://github.com/the-hcma/domesti-bot/issues/336)) ([d2331ab](https://github.com/the-hcma/domesti-bot/commit/d2331abd62845bbae727f6652b850c019c93f0f5))
* show dismissible SMTP failure banner in web UI ([#341](https://github.com/the-hcma/domesti-bot/issues/341)) ([3c90faa](https://github.com/the-hcma/domesti-bot/commit/3c90faa8997ec49d39a4a348df8900db13536e03))
* treat unresponsive off actions as probable successes ([#340](https://github.com/the-hcma/domesti-bot/issues/340)) ([ffa8931](https://github.com/the-hcma/domesti-bot/commit/ffa893159be00bdc079265ec3dc9dbffec34780d))


### Bug Fixes

* emit geofence leave edges after history reconcile and poor GPS ([#344](https://github.com/the-hcma/domesti-bot/issues/344)) ([a5a349c](https://github.com/the-hcma/domesti-bot/commit/a5a349c0b711c606bfb35e68533bba7848c77df1))
* fire geofence enter edge after sustained GPS inside streak ([#339](https://github.com/the-hcma/domesti-bot/issues/339)) ([644dcfb](https://github.com/the-hcma/domesti-bot/commit/644dcfb4f2aba85b6ea401359d51c1f0344a5e68))
* harden rule notification email dispatch and logging ([#338](https://github.com/the-hcma/domesti-bot/issues/338)) ([2a89ccf](https://github.com/the-hcma/domesti-bot/commit/2a89ccf458d430fa09e68b230fabfafe594dc696))

## [1.6.0](https://github.com/the-hcma/domesti-bot/compare/v1.5.0...v1.6.0) (2026-06-23)


### Features

* away shutdown rules, outside dwell, and multi-email notify ([#334](https://github.com/the-hcma/domesti-bot/issues/334)) ([57c5fe9](https://github.com/the-hcma/domesti-bot/commit/57c5fe902655cb8a91e9c4e87f8b2315ead4fec6))

## [1.5.0](https://github.com/the-hcma/domesti-bot/compare/v1.4.0...v1.5.0) (2026-06-23)


### Features

* WiFi dwell presence, history backfill, and 10m scheduled eval ([#332](https://github.com/the-hcma/domesti-bot/issues/332)) ([35e005b](https://github.com/the-hcma/domesti-bot/commit/35e005b068e8169e88eaef6c440bd1fe1dd5d93d))

## [1.4.0](https://github.com/the-hcma/domesti-bot/compare/v1.3.0...v1.4.0) (2026-06-21)


### Features

* presence conn type, accuracy edge gate, and low-accuracy WiFi home ([#327](https://github.com/the-hcma/domesti-bot/issues/327)) ([8b99b5c](https://github.com/the-hcma/domesti-bot/commit/8b99b5c4de31e347a2885acfc16da575a4ce15d2))


### Documentation

* add away-pause-media example and refresh rule plan ([#324](https://github.com/the-hcma/domesti-bot/issues/324)) ([34becc3](https://github.com/the-hcma/domesti-bot/commit/34becc35f298f70378edaaa57e5c5bbfcf246504))

## [1.3.0](https://github.com/the-hcma/domesti-bot/compare/v1.2.1...v1.3.0) (2026-06-20)


### Features

* extend rule device conditions to Sonos and Vizio ([#322](https://github.com/the-hcma/domesti-bot/issues/322)) ([65d46f4](https://github.com/the-hcma/domesti-bot/commit/65d46f48128c56309000773d54582d29d87bebca))

## [1.2.1](https://github.com/the-hcma/domesti-bot/compare/v1.2.0...v1.2.1) (2026-06-20)


### Bug Fixes

* **ci:** verify PyPI release via version JSON API ([#321](https://github.com/the-hcma/domesti-bot/issues/321)) ([9fb8ec3](https://github.com/the-hcma/domesti-bot/commit/9fb8ec3d484ab4bff3a99a55e889ad60a14b5f82))


### Documentation

* geofence persisted state vs location-history pruning ([#318](https://github.com/the-hcma/domesti-bot/issues/318)) ([f22ebae](https://github.com/the-hcma/domesti-bot/commit/f22ebae04c98fc64a7d8f9a334703ec77c122c47))

## [1.2.0](https://github.com/the-hcma/domesti-bot/compare/v1.1.0...v1.2.0) (2026-06-19)


### Features

* persist geofence transition state across restarts ([#317](https://github.com/the-hcma/domesti-bot/issues/317)) ([85af553](https://github.com/the-hcma/domesti-bot/commit/85af55376e2f09107c0edc5fe4ed65438b749aff))
* seed geofence dwell state from location history on restart ([#316](https://github.com/the-hcma/domesti-bot/issues/316)) ([43f87ce](https://github.com/the-hcma/domesti-bot/commit/43f87ce9bec6fdbd3aa8051b90a40c518bd9c27e))


### Documentation

* modernize user-facing documentation ([#313](https://github.com/the-hcma/domesti-bot/issues/313)) ([b71c8c4](https://github.com/the-hcma/domesti-bot/commit/b71c8c446ca0fe97c039c265df8a52c9fe685bfd))

## [1.1.0](https://github.com/the-hcma/domesti-bot/compare/v1.0.1...v1.1.0) (2026-06-19)


### Features

* add device-state rule conditions ([#309](https://github.com/the-hcma/domesti-bot/issues/309)) ([f001abd](https://github.com/the-hcma/domesti-bot/commit/f001abd96b0dae5900b258286d6d9bdaea44ec37))
* add diagnostic logging for rule evaluation skips and fires ([#283](https://github.com/the-hcma/domesti-bot/issues/283)) ([df276b4](https://github.com/the-hcma/domesti-bot/commit/df276b4d651263855331816b06e736673b287d59))
* add file-backed automation rules bundle and evaluator plan ([#221](https://github.com/the-hcma/domesti-bot/issues/221)) ([5875ee4](https://github.com/the-hcma/domesti-bot/commit/5875ee48c5102c083b74977e6076e3d4a06184c2))
* add fire_once_per_local_day for scheduled rules ([#311](https://github.com/the-hcma/domesti-bot/issues/311)) ([42fbd12](https://github.com/the-hcma/domesti-bot/commit/42fbd12cb7122ef1cc411b104d2ce5f215cb5bde))
* add geofence inside dwell condition for rules ([#307](https://github.com/the-hcma/domesti-bot/issues/307)) ([c9525fe](https://github.com/the-hcma/domesti-bot/commit/c9525fe1d26227bd94d4ee69120361b8ed56acf3))
* add participant map legend, colors, and live location polling ([#216](https://github.com/the-hcma/domesti-bot/issues/216)) ([5e83925](https://github.com/the-hcma/domesti-bot/commit/5e8392574d9a9b2cb3504c565084c0369aabdbf2))
* add scheduled cron rule trigger ([#308](https://github.com/the-hcma/domesti-bot/issues/308)) ([088feb1](https://github.com/the-hcma/domesti-bot/commit/088feb1b5bcc74ad3396c62ef036e0cd226e7d51))
* add shared toast and confirm dialog helpers ([#203](https://github.com/the-hcma/domesti-bot/issues/203)) ([2c74d46](https://github.com/the-hcma/domesti-bot/commit/2c74d4615c0a5b8be11dd987a902cd60aded9ea8))
* add Vizio SmartCast TV support ([#268](https://github.com/the-hcma/domesti-bot/issues/268)) ([cf9b874](https://github.com/the-hcma/domesti-bot/commit/cf9b874fe439bf50074a9ecb21873dd745a80e08))
* add vizio-set-mac REPL command ([#273](https://github.com/the-hcma/domesti-bot/issues/273)) ([624113a](https://github.com/the-hcma/domesti-bot/commit/624113a1df058233fb89a9812e76b7e96f0c5e08))
* add wait-for-copilot-review script and ship loop rule ([#303](https://github.com/the-hcma/domesti-bot/issues/303)) ([df9bddb](https://github.com/the-hcma/domesti-bot/commit/df9bddb79ac24ba7bffa9646d397a6fb39f708de))
* Automations hub — mail tab, notify email, fixed dialog ([#181](https://github.com/the-hcma/domesti-bot/issues/181)) ([c23d79e](https://github.com/the-hcma/domesti-bot/commit/c23d79eac23fe546b18d3aae3cce158f8e1cae74))
* Automations hub v2 — Rules tab, mail domain, my-tracks participants ([#182](https://github.com/the-hcma/domesti-bot/issues/182)) ([a01f755](https://github.com/the-hcma/domesti-bot/commit/a01f755fe201510a8599cd29256d5238248e4751))
* compact mail settings form with paired fields ([#195](https://github.com/the-hcma/domesti-bot/issues/195)) ([93245ec](https://github.com/the-hcma/domesti-bot/commit/93245ec48e3fa545bdd7b516241293cf8d988a58))
* conditions windows, rule ids, and My Tracks sync settings ([#192](https://github.com/the-hcma/domesti-bot/issues/192)) ([f77a398](https://github.com/the-hcma/domesti-bot/commit/f77a3981211224ef805516e6fffd43b80ac50a22))
* defer geofence edge rules when GPS accuracy is poor ([#290](https://github.com/the-hcma/domesti-bot/issues/290)) ([8267591](https://github.com/the-hcma/domesti-bot/commit/826759147405cf9644e47f48b1dc2e6527109af1))
* desktop Rules hub UI with mock data source ([#175](https://github.com/the-hcma/domesti-bot/issues/175)) ([c73b4ee](https://github.com/the-hcma/domesti-bot/commit/c73b4ee8c135e68095b829057a3954575aec1969))
* dispatch automation rule device actions and notifications ([#248](https://github.com/the-hcma/domesti-bot/issues/248)) ([ef26aea](https://github.com/the-hcma/domesti-bot/commit/ef26aea9460e07961c42dfc2e16135951553e1a4))
* human-readable rule cards with top enable toggle ([#200](https://github.com/the-hcma/domesti-bot/issues/200)) ([e42a201](https://github.com/the-hcma/domesti-bot/commit/e42a2011c8a468d7715555c16d7e8040097e93b4))
* improve My Tracks sync UI and participant locations ([#202](https://github.com/the-hcma/domesti-bot/issues/202)) ([e52a116](https://github.com/the-hcma/domesti-bot/commit/e52a116ebcad89afa2d24b10f30afc53e35c79a7))
* Leaflet geofence map editor in Rules hub ([#176](https://github.com/the-hcma/domesti-bot/issues/176)) ([dbe24e0](https://github.com/the-hcma/domesti-bot/commit/dbe24e0a5fc54413fbe6a7664f4988e5cbb3c523))
* link Conditions sunset/sunrise home location to geofence ([#186](https://github.com/the-hcma/domesti-bot/issues/186)) ([c0d7d57](https://github.com/the-hcma/domesti-bot/commit/c0d7d575d82436d540a9e255d0fa03cae4edf37e))
* live My Tracks sync via generic admin export routes ([#197](https://github.com/the-hcma/domesti-bot/issues/197)) ([d541675](https://github.com/the-hcma/domesti-bot/commit/d541675dff7a9e4f0cfc3f2b0720dc53505fab91))
* my-tracks pairing, location webhooks, and history retention ([#209](https://github.com/the-hcma/domesti-bot/issues/209)) ([0de44d9](https://github.com/the-hcma/domesti-bot/commit/0de44d98e3f1673398c69d220913fa6e85d30fa5))
* operational SMTP settings API and Mail tab wire-up ([#183](https://github.com/the-hcma/domesti-bot/issues/183)) ([549527a](https://github.com/the-hcma/domesti-bot/commit/549527aa15b980f17ee1cc56f8a61bd948604706))
* persist rule last-fired status and debounce geofence re-entry ([#282](https://github.com/the-hcma/domesti-bot/issues/282)) ([ec075c0](https://github.com/the-hcma/domesti-bot/commit/ec075c072008aebf8a4f24155d531871b62f800a))
* polish automations UI layout, toasts, and rule editor ([#204](https://github.com/the-hcma/domesti-bot/issues/204)) ([848c1cf](https://github.com/the-hcma/domesti-bot/commit/848c1cfab1e3396ae9b6ce3ae183b0803a595754))
* read-only automation rule inspector in Automations UI ([#227](https://github.com/the-hcma/domesti-bot/issues/227)) ([84db43e](https://github.com/the-hcma/domesti-bot/commit/84db43ef9e16b57b4dc30f0827d62563142eb4dd))
* refine Rules hub UX — menu, status drill-down, device actions ([#179](https://github.com/the-hcma/domesti-bot/issues/179)) ([e4599e2](https://github.com/the-hcma/domesti-bot/commit/e4599e23b781380ab34675a6053d1bf2baa84649))
* rule evaluator on location ingest with server runtime singleton ([#249](https://github.com/the-hcma/domesti-bot/issues/249)) ([e5896e3](https://github.com/the-hcma/domesti-bot/commit/e5896e3d0d6e3eecda714773d453f77e65667131))
* Rules hub polish — wider layout, Conditions tab, menu fix ([#180](https://github.com/the-hcma/domesti-bot/issues/180)) ([f678932](https://github.com/the-hcma/domesti-bot/commit/f6789325c6af425e0b39737b3a213b226ef75149))
* rules UI types, mock fixtures, and data source ([#174](https://github.com/the-hcma/domesti-bot/issues/174)) ([4a70dad](https://github.com/the-hcma/domesti-bot/commit/4a70dadcb6f0c9a8201073ee3a398382d0c6f639))
* serve automation rules JSON to Automations UI ([#222](https://github.com/the-hcma/domesti-bot/issues/222)) ([339c784](https://github.com/the-hcma/domesti-bot/commit/339c784ac0ebd7d34d6b221471c48faeea298f57))
* server rule conditions and GET /v1/rules/status ([#229](https://github.com/the-hcma/domesti-bot/issues/229)) ([c64677c](https://github.com/the-hcma/domesti-bot/commit/c64677c3cebdfdd81e5e016f82476bfe6c285d62))
* show Vizio MAC address in Setup panel ([#272](https://github.com/the-hcma/domesti-bot/issues/272)) ([2e66ef7](https://github.com/the-hcma/domesti-bot/commit/2e66ef756aa1d1a1b7bd9b2561bab021ed90dc81))
* simplify Status tab and drill into Rules rule detail ([#230](https://github.com/the-hcma/domesti-bot/issues/230)) ([506c584](https://github.com/the-hcma/domesti-bot/commit/506c584c12f07888794e540de4c470a0a9acb622))
* tabbed Settings hub and domain-only My Tracks config ([#196](https://github.com/the-hcma/domesti-bot/issues/196)) ([7259079](https://github.com/the-hcma/domesti-bot/commit/7259079146f3a7919f4983f3de2b1f261133e31c))
* unified participant presence map with filters and zoom ([#185](https://github.com/the-hcma/domesti-bot/issues/185)) ([9761ca1](https://github.com/the-hcma/domesti-bot/commit/9761ca15419a045d097a365708216b1c9abfa125))
* wire Mail tab to live SMTP API ([#187](https://github.com/the-hcma/domesti-bot/issues/187)) ([6eade78](https://github.com/the-hcma/domesti-bot/commit/6eade78421ea1f9147cf19ee5f7b6b40d446c626))


### Bug Fixes

* align log columns and show locale timestamps in UI ([#214](https://github.com/the-hcma/domesti-bot/issues/214)) ([9e98a5d](https://github.com/the-hcma/domesti-bot/commit/9e98a5dd8b3dce2c2bbfe5494bb5fa25b8ae38e5))
* Automations dialog theme and default mail domain from UI URL ([#190](https://github.com/the-hcma/domesti-bot/issues/190)) ([f830441](https://github.com/the-hcma/domesti-bot/commit/f830441e5d2024f9ccbdd6d002478071c6e8dcbc))
* bound after-sunset and before-sunrise windows at midnight ([#194](https://github.com/the-hcma/domesti-bot/issues/194)) ([f5a70b7](https://github.com/the-hcma/domesti-bot/commit/f5a70b707725a4ab2ecb0cdb0c3174e0efcaef27))
* cap Vizio bootstrap WoL wait and improve discovery logging ([#295](https://github.com/the-hcma/domesti-bot/issues/295)) ([9a3c235](https://github.com/the-hcma/domesti-bot/commit/9a3c23524995e4997327744d257c38c5d472e850))
* **ci:** delete Graphite MQ staging branches when PR closes ([#233](https://github.com/the-hcma/domesti-bot/issues/233)) ([331c597](https://github.com/the-hcma/domesti-bot/commit/331c597dd6c78d03b827469b0a558ead6c8ccdc7))
* **ci:** key CI concurrency on head SHA ([#240](https://github.com/the-hcma/domesti-bot/issues/240)) ([a28eb49](https://github.com/the-hcma/domesti-bot/commit/a28eb492b9712268b04e09364e7acc3038131191))
* **ci:** run CI after Graphite restack without push fallback ([#252](https://github.com/the-hcma/domesti-bot/issues/252)) ([63c9757](https://github.com/the-hcma/domesti-bot/commit/63c9757c0ea8cc3bdb1208e31d303b307edae767))
* **ci:** run CI on Graphite restack (push fallback + guard dedup) ([#255](https://github.com/the-hcma/domesti-bot/issues/255)) ([64f4d34](https://github.com/the-hcma/domesti-bot/commit/64f4d34c501fb8b8ee89f47701b84a5b0aa9cb23))
* **ci:** skip duplicate push CI when an open PR exists ([#275](https://github.com/the-hcma/domesti-bot/issues/275)) ([744d054](https://github.com/the-hcma/domesti-bot/commit/744d0549322654834757504bd1fa519161e56e5c))
* **ci:** skip duplicate push CI when an open PR exists ([#300](https://github.com/the-hcma/domesti-bot/issues/300)) ([1b4b2d0](https://github.com/the-hcma/domesti-bot/commit/1b4b2d0dd008507da0b303a6cb6aba7d0fa03156))
* **ci:** sync canonical secret-scan script ([#223](https://github.com/the-hcma/domesti-bot/issues/223)) ([d1e4894](https://github.com/the-hcma/domesti-bot/commit/d1e4894bfce332e4576a7b73645ef5cd8f7c799b))
* **ci:** sync canonical secret-scan script ([#231](https://github.com/the-hcma/domesti-bot/issues/231)) ([e1a6a5a](https://github.com/the-hcma/domesti-bot/commit/e1a6a5abe0ab28ee9b1ca85534393207c51292d2))
* **ci:** use canonical secret-scan job ([#224](https://github.com/the-hcma/domesti-bot/issues/224)) ([f214545](https://github.com/the-hcma/domesti-bot/commit/f214545044d877c5797f33de8704dd53e03cf04a))
* complete automations UI polish gaps from review ([#207](https://github.com/the-hcma/domesti-bot/issues/207)) ([0d51e53](https://github.com/the-hcma/domesti-bot/commit/0d51e53aaadcbf14aced4a179a01e628d37e3c52))
* contain map tooltips and add Participants hover details ([#188](https://github.com/the-hcma/domesti-bot/issues/188)) ([268a643](https://github.com/the-hcma/domesti-bot/commit/268a643b771a650dcb3bfb1a2ddff27a1eca1161))
* demote static asset access logs including 404s to DEBUG ([#284](https://github.com/the-hcma/domesti-bot/issues/284)) ([ff8c06e](https://github.com/the-hcma/domesti-bot/commit/ff8c06e6862ad481105f0766fa8d4baf530112a3))
* **deps:** batch updates including CVE fixes ([#299](https://github.com/the-hcma/domesti-bot/issues/299)) ([c70d993](https://github.com/the-hcma/domesti-bot/commit/c70d9933d4c4cdc934d8023e402cc99fc6547fc9))
* edge-triggered automations status and roster display names ([#257](https://github.com/the-hcma/domesti-bot/issues/257)) ([cc4e9ad](https://github.com/the-hcma/domesti-bot/commit/cc4e9ad520c070fda32257709534e46c3f1c69af))
* gate geofence edge state on GPS accuracy ([#293](https://github.com/the-hcma/domesti-bot/issues/293)) ([28c6bc7](https://github.com/the-hcma/domesti-bot/commit/28c6bc75a402ac6b23f24fe2f35ecae0617cdb96))
* handle unreachable Vizio TVs in power state and turn-off ([#270](https://github.com/the-hcma/domesti-bot/issues/270)) ([0638e9d](https://github.com/the-hcma/domesti-bot/commit/0638e9d5f0d501618487d07170f87aec458dc6a4))
* harden My Tracks sync and tighten geofence tab layout ([#201](https://github.com/the-hcma/domesti-bot/issues/201)) ([2331ee7](https://github.com/the-hcma/domesti-bot/commit/2331ee7fa0bc435109e973558ef4e4471a080123))
* host participant map tooltips on shell to keep OSM tiles ([#219](https://github.com/the-hcma/domesti-bot/issues/219)) ([db0b87e](https://github.com/the-hcma/domesti-bot/commit/db0b87e42506517f8241082666bbe97e2ac2d294))
* improve Vizio settings UX and hot-reload after pairing ([#269](https://github.com/the-hcma/domesti-bot/issues/269)) ([67ccd8c](https://github.com/the-hcma/domesti-bot/commit/67ccd8c29354bda2d653a57912aed9031333b698))
* include accuracy_m in presence location store logs ([#289](https://github.com/the-hcma/domesti-bot/issues/289)) ([e62374a](https://github.com/the-hcma/domesti-bot/commit/e62374a4feec9c8fb25ba8372ab7cd3223f28aa2))
* instant Broken tooltip and address PR 261 review feedback ([#264](https://github.com/the-hcma/domesti-bot/issues/264)) ([ee7e280](https://github.com/the-hcma/domesti-bot/commit/ee7e280b3cd3e70f42d0a1250a7b044dcfbbe14f))
* keep cached Vizio TVs on rediscover and fast offline bootstrap ([#278](https://github.com/the-hcma/domesti-bot/issues/278)) ([a800a41](https://github.com/the-hcma/domesti-bot/commit/a800a41ecdfa1f2a754ca1cf8ddcd8d97dbf569c))
* keep UI responsive during offline Vizio polls ([#302](https://github.com/the-hcma/domesti-bot/issues/302)) ([e97f0f2](https://github.com/the-hcma/domesti-bot/commit/e97f0f2f7857f2983a5a4d06e9534568f74eae77))
* log and surface rule execution failures when notify fails ([#267](https://github.com/the-hcma/domesti-bot/issues/267)) ([d89d1a6](https://github.com/the-hcma/domesti-bot/commit/d89d1a6b774de22680cd7e58165f3c6254b20586))
* log transient device poll connect failures without tracebacks ([#286](https://github.com/the-hcma/domesti-bot/issues/286)) ([c534eae](https://github.com/the-hcma/domesti-bot/commit/c534eae842af21bf40de2b6d0530895c8ff2d838))
* Mail save/reset and instance link in test email ([#189](https://github.com/the-hcma/domesti-bot/issues/189)) ([8843f90](https://github.com/the-hcma/domesti-bot/commit/8843f90f1e46d8fa7c44345c71ee64bb691ed01e))
* polish My Tracks settings tab UX ([#212](https://github.com/the-hcma/domesti-bot/issues/212)) ([6305228](https://github.com/the-hcma/domesti-bot/commit/6305228332d8f41cab0610a94e476dee33d797da))
* polish my-tracks pairing UI and logging ([#210](https://github.com/the-hcma/domesti-bot/issues/210)) ([26c5d72](https://github.com/the-hcma/domesti-bot/commit/26c5d7222e0a38d80265b68221ca79e1a500e7dc))
* render participant presence mini maps on Status and Participants tabs ([#184](https://github.com/the-hcma/domesti-bot/issues/184)) ([aad7234](https://github.com/the-hcma/domesti-bot/commit/aad723424d883ae3f1d0b4af920c409cc1d10cff))
* replace Enabled toggle with Broken badge and accept relay SMTP ([#261](https://github.com/the-hcma/domesti-bot/issues/261)) ([2529d4b](https://github.com/the-hcma/domesti-bot/commit/2529d4b78dd6703944114936da3ebcd0f22cccfd))
* restore etc/systemd unit templates ([#305](https://github.com/the-hcma/domesti-bot/issues/305)) ([ddf9d51](https://github.com/the-hcma/domesti-bot/commit/ddf9d510d3127bdfb6e6db1db503a7d46f650af6))
* restore OSM tiles on Participants map and refresh My Tracks plan ([#217](https://github.com/the-hcma/domesti-bot/issues/217)) ([a83c446](https://github.com/the-hcma/domesti-bot/commit/a83c4465669a45b7270ae64a22746b2ec620dc99))
* show Henrique and geofence labels on presence map tooltips ([#226](https://github.com/the-hcma/domesti-bot/issues/226)) ([a24c026](https://github.com/the-hcma/domesti-bot/commit/a24c026df18c27be1826e708c31aca01b3964697))
* show TV tile icons for Vizio instead of bulb fallback ([#280](https://github.com/the-hcma/domesti-bot/issues/280)) ([31232dd](https://github.com/the-hcma/domesti-bot/commit/31232ddde674af743a1e124ce66fa8d7d8a56fa4))
* stop clipping participant map tooltips at map edges ([#218](https://github.com/the-hcma/domesti-bot/issues/218)) ([9ca4bf2](https://github.com/the-hcma/domesti-bot/commit/9ca4bf233b40e6744106326d547aea3e7d26ec18))
* stop relay key field stretching in My Tracks tab ([#213](https://github.com/the-hcma/domesti-bot/issues/213)) ([a1a72c3](https://github.com/the-hcma/domesti-bot/commit/a1a72c397fe39d51d3a372b1a37ae41521b7d87d))
* streamline My Tracks settings tab ([#211](https://github.com/the-hcma/domesti-bot/issues/211)) ([4b0f12a](https://github.com/the-hcma/domesti-bot/commit/4b0f12a8be49c2a8adfdf9744274239fc90fa68d))
* tag uvicorn startup/shutdown log lines as lifecycle ([#285](https://github.com/the-hcma/domesti-bot/issues/285)) ([ee1b6dd](https://github.com/the-hcma/domesti-bot/commit/ee1b6dd97334215044456b4089854393cb16fb5c))
* tighten logging format and demote HTTP access below INFO ([#215](https://github.com/the-hcma/domesti-bot/issues/215)) ([e12bf56](https://github.com/the-hcma/domesti-bot/commit/e12bf56876ce452cde8554cf061b47bbc6278561))
* treat Vizio cast playback as on and preserve state on API errors ([#292](https://github.com/the-hcma/domesti-bot/issues/292)) ([759ae59](https://github.com/the-hcma/domesti-bot/commit/759ae59677524e2038839a7c3662ba5c3af84878))
* use map hit-testing for participant hover tooltips ([#220](https://github.com/the-hcma/domesti-bot/issues/220)) ([c4af2cf](https://github.com/the-hcma/domesti-bot/commit/c4af2cf36269221f2ac1c49f2682ab0b19fcc03f))
* validate rule references and resolve roster user ids ([#258](https://github.com/the-hcma/domesti-bot/issues/258)) ([62ce539](https://github.com/the-hcma/domesti-bot/commit/62ce5395318bf71d302fe3efe2513c3542256647))
* widen participant map hover tooltips ([#191](https://github.com/the-hcma/domesti-bot/issues/191)) ([524c9eb](https://github.com/the-hcma/domesti-bot/commit/524c9eb1049c75768ed1518e6e2489b16b8e67ce))
* WoL bootstrap probe for Vizio and default unreachable polls to off ([#294](https://github.com/the-hcma/domesti-bot/issues/294)) ([bd41bbd](https://github.com/the-hcma/domesti-bot/commit/bd41bbd45d26cacc6a4ad2393946f3b90dd2e15d))


### Documentation

* add evening lights-off scheduled example rule ([#310](https://github.com/the-hcma/domesti-bot/issues/310)) ([7dcc68e](https://github.com/the-hcma/domesti-bot/commit/7dcc68eb8e1cb76a51fe90e30693f2ca4706e892))
* add my-tracks integration plan for domesti-bot ([#208](https://github.com/the-hcma/domesti-bot/issues/208)) ([0480b51](https://github.com/the-hcma/domesti-bot/commit/0480b518ffb1c9f4189168ca276dc54bd19a9018))
* clarify Dependabot version cooldown vs early security updates ([#169](https://github.com/the-hcma/domesti-bot/issues/169)) ([b30f25e](https://github.com/the-hcma/domesti-bot/commit/b30f25e248f141cc626e3126514ed2db44ead269))
* finish user/location nomenclature in planning docs ([#245](https://github.com/the-hcma/domesti-bot/issues/245)) ([235da07](https://github.com/the-hcma/domesti-bot/commit/235da07cc4da7c8ea646625220783d858ffb2f79))
* forbid agent writes on main worktree ([#171](https://github.com/the-hcma/domesti-bot/issues/171)) ([950ade5](https://github.com/the-hcma/domesti-bot/commit/950ade5809961910baafe73226f615d2d2a13928))
* plan scheduled rules, dwell, and device-state conditions ([#291](https://github.com/the-hcma/domesti-bot/issues/291)) ([499d82f](https://github.com/the-hcma/domesti-bot/commit/499d82f024ad2cdafa60dc61f6d744b9c48a4652))
* rule engine plan — UI-first mocks, my-tracks sunset, geofence map ([#172](https://github.com/the-hcma/domesti-bot/issues/172)) ([80ef480](https://github.com/the-hcma/domesti-bot/commit/80ef4807179b2a5ddfd4f8cd258238b616648627))
* rule engine plan for inspector, asyncio, and TS removal ([#225](https://github.com/the-hcma/domesti-bot/issues/225)) ([96faa0a](https://github.com/the-hcma/domesti-bot/commit/96faa0aa31f3c989107f885327647cb9459be36d))
* sync Phase 2a plan; remove while_true trigger ([#312](https://github.com/the-hcma/domesti-bot/issues/312)) ([86b1ff4](https://github.com/the-hcma/domesti-bot/commit/86b1ff4323a0cfc2aa84eb86e1c030834821caab))
* user/location nomenclature in planning docs ([#237](https://github.com/the-hcma/domesti-bot/issues/237)) ([8293cf4](https://github.com/the-hcma/domesti-bot/commit/8293cf449f563d1d68894893df4747fec83f2769))

## [1.0.1](https://github.com/the-hcma/domesti-bot/compare/v1.0.0...v1.0.1) (2026-05-27)


### Bug Fixes

* hard-fail on invalid domesti-bot.config.json ([#144](https://github.com/the-hcma/domesti-bot/issues/144)) ([52ecad5](https://github.com/the-hcma/domesti-bot/commit/52ecad5365b8c845b8fa68fa20fb5d16b6ddba8d))

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
