# CTBRec Discovery — Complete Development Handoff

**Current release:** v0.4.0 — Real-Data Adapter + Local Ecosystem Reuse
**Updated:** 2026-09-20
**Service port:** 8793 (loopback by default)
**SQLite schema:** v2 (unchanged)

## 1. Standing user delivery requirements

Every future development response that changes this project must provide all of the following, even for interim/WIP work:

1. complete cumulative **FULL SOURCE ZIP**;
2. safe **PATCH / UPDATE ZIP** from the immediately prior release;
3. fully current Markdown handoff/context file;
4. validation/test report and SHA-256 checksums;
5. email to `christianpwire@gmail.com` in the existing Gmail thread **CTBRec Discovery Build Updates**.

Gmail blocks ordinary source ZIPs containing script types. The established email delivery workaround is to attach base64-encoded `.txt` representations of the exact ZIP bytes, plus the handoff/test/checksum files. The normal ZIPs are still delivered directly in ChatGPT.

Never ship active `discovery_config.json`, SQLite/runtime state, logs, cookies, Recu session material, affiliate WM IDs, PINs/passwords/secrets or other user-specific private data.

## 2. Product goal

Build a personalized CTBRec discovery layer with three deliberately separate prediction tasks:

1. **Model affinity** — surface new public accounts the user is likely to value, anchored primarily in the user's own Favorite / Likely Favorite / Continue / Low Priority / Ignore history.
2. **Public account continuity** — surface likely username/account continuations of already-known models while keeping identity merge confirmation explicit and avoiding home-grown biometric identification.
3. **Session opportunity** — future separate layer for unusually interesting broadcasts of already-promising models; viewer/tip/activity anomalies must not contaminate underlying taste affinity.

The system must be explainable, background-safe, reversible and integrated with existing CTBRec tooling rather than duplicating expensive scans/browser sessions/network work.

## 3. Authoritative neighboring applications

### Mobile Reviewer / Mosaic Sorter

- current user field server reported `CTBRecMobile/2.15.5 Python/3.10.11` on `127.0.0.1:8787`;
- owns the recording-disk catalog and mosaic/action workflows;
- `mobile_catalog_cache.json` is the preferred Discovery model-universe input;
- Discovery must not create a second recurring recording-library scan;
- existing Reviewer auth/session boundary must remain intact.

### Live Mobile Control

- identity-verified JVM bridge: `127.0.0.1:8791`;
- Python/web controller: `127.0.0.1:8792`;
- controls the exact intended running CTBRec installation;
- already has the proven complete Chaturbate Users Online affiliate polling/parser internally;
- `/api/models` exposes the tracked model set plus current operational/public room metadata when available;
- media endpoints/player logic exist for exact tracked models;
- normal mutations go through the verified bridge, not `models.json`.

### Rapid Model Sorter

- current architecture uses Mobile Reviewer caches rather than rescanning media;
- owns local Recu intelligence and `runtime/recu_local_archive.json`;
- local Best Moments media/sidecars live under `runtime/recu_best_moments/`;
- live/authenticated Recu access is already brokered through the existing authorized workflow;
- Discovery must read local outputs rather than compete for the Recu browser/session.

## 4. Release history

### v0.1.0 — foundation

- companion service on 8793;
- SQLite/WAL identity/account/evidence store;
- first interpretable affinity ranker;
- non-biometric continuity matcher;
- evidence inbox/API;
- Discover + Possible Returns UI.

### v0.2.0 — integration readiness

- evidence dedupe keys and freshness decay;
- persistent continuity rejection decisions;
- feedback Undo;
- inert CTBRec action outbox;
- Reviewer/catalog health adapter;
- safe overlay/state-preservation packaging.

### v0.3.0 — background collectors

- optional direct official Chaturbate affiliate collector;
- local Recu discovery export;
- local neighbor export;
- local continuity export;
- local Recu archive enrichment;
- collector scheduling/source-state UI;
- new-account signal restricted to context lane;
- field-testable standalone UI.

### v0.4.0 — CURRENT

Built in response to the first real-PC v0.3 validation. It fixes real input schemas, stops heavy collectors from blocking startup, reuses Live Control locally for known/tracked models, adds global eligibility filtering and makes recommendation readiness/score semantics visible.

## 5. v0.3 field validation that motivated v0.4

The user successfully ran v0.3 and confirmed feedback/Undo/persistence/UI behavior worked. Status exposed two critical data defects:

### Mobile Reviewer catalog defect

The configured file existed, was ~2.2 MB, and status identified top-level keys:

- `updated_at`;
- `roots_file`;
- `catalog`.

But Discovery reported:

`0/0 imported; 0 errors; mobile_catalog_cache.json`

Therefore the real catalog was not contributing preference/history data.

### Recu cache defect

Both configured local cache paths existed and the source reported `files: 2, errors: 0`, but `imported: 0`.

Therefore the local Recu parser was not matching the actual archive/cache structures.

### Visible recommendation consequence

Top cards showed approximately:

- preference match: 50;
- neighbor: 0;
- Recu momentum: 0;
- context: ~45;
- exploration: ~100;
- total score ~35.

Many were female accounts despite the user's stated preference. This established that v0.3's visible list was mostly neutral baseline/context/exploration from the 99 previously imported new-account candidates, not a mature personalized ranking.

This field test is considered a **successful smoke test** of the app shell/persistence and a **failed real-data personalization test**. v0.4 addresses the data adapters before adding more ranking complexity.

## 6. v0.4 process architecture

```text
Mobile Reviewer catalog cache (read only) ──────────┐
                                                     │
Live Control /api/models (loopback only) ───────────┤
                                                     ├─> Discovery SQLite/WAL
Rapid Sorter / Reviewer Recu caches (read only) ────┤      │
                                                     │      ├─> affinity ranker
Optional local evidence exports (read only) ────────┤      ├─> continuity matcher
                                                     │      └─> diagnostics/UI
Official affiliate API (manual fallback only) ──────┘

Discovery feedback ─> action_outbox (execute:false) ─> FUTURE exact verified bridge adapter
```

Discovery still has no mutation consumer.

## 7. Startup / background-work correction

### v0.3 bug

`App.__init__` ran `engine.sync_local_sources()` before the HTTP server bound its socket or printed the listening message. If the direct affiliate source was enabled, first startup could spend minutes paginating the official feed while the console showed only a blinking cursor.

### v0.4 behavior

`App.__init__` is construction-only. `main()` now:

1. loads config + creates engine;
2. binds the `ThreadingHTTPServer`;
3. prints the listening URL immediately;
4. prints that initial sync is queued;
5. starts the background thread;
6. serves requests while collectors run.

Manual Sync is also asynchronous. `DiscoveryEngine.request_sync()` creates one background worker and exposes progress/state via `/api/sync-status`. A sync lock prevents overlapping complete sync passes.

Heavy external work must never be moved back into startup or a browser-request hot path.

## 8. Real Mobile Reviewer cache importer

Code: `discovery/importers.py`.

v0.4 explicitly understands:

```json
{
  "updated_at": "...",
  "roots_file": "...",
  "catalog": {
    "username": [ ... folder/root rows ... ]
  }
}
```

The `catalog` key is treated as the model-universe container. For list-valued account entries, the map key supplies the username and rows are aggregated instead of silently ignored.

Possible aggregate metadata includes:

- folder count;
- total bytes;
- file count;
- per-drive bytes;
- useful seen/activity timestamps when supplied.

The old generic list/map/camelCase compatibility paths remain.

### File-fingerprint optimization

The catalog importer now stores cursor:

`v4:<mtime_ns>:<size>`

An unchanged catalog returns `unchanged` without reparsing/importing the multi-megabyte file every scheduler tick. The v4 prefix intentionally forces one real re-read after upgrading from v0.3.

## 9. Live Control reuse collector

Code: `LiveControlModelsCollector` in `discovery/collectors.py`.

Default source/config:

- source name: `live_control_models`;
- base URL: `http://127.0.0.1:8792`;
- endpoint: `/api/models`;
- default enabled: true;
- default poll: 60 seconds;
- network scope: loopback only.

It can consume common list/object wrappers and extracts:

- username/name/model/URL slug;
- favorite/current workflow priority where present;
- online/public state;
- `affiliateRoomInfo` metadata already gathered by Live Control.

`affiliateRoomInfo` can enrich gender, tags, subject, viewers, followers, live duration, language/location/HD/new/public state without another Chaturbate request.

### Critical limitation

Live Control `/api/models` exposes CTBRec-tracked models, not the complete untracked global affiliate universe. Therefore v0.4 cannot honestly replace global new-account discovery with this endpoint alone.

Policy:

- known/tracked metadata: reuse Live Control;
- recurring standalone direct affiliate polling: disabled by default;
- direct official affiliate collector: manual/optional one-shot fallback;
- mature target: patch Live Control (when its exact source is available) to expose a sanitized complete accepted affiliate snapshot locally, then remove duplicate global fetching.

Do not claim the duplicate global-feed problem is fully solved in v0.4.

## 10. Direct affiliate fallback

`chaturbate_affiliate` remains in the code for official-API new-account discovery.

v0.4 behavior differs from v0.3:

- shipping/default scheduled state: disabled;
- if active config contains a WM ID, Sources may display `Run one-shot`;
- an explicit forced run temporarily instantiates an enabled collector in memory;
- persisted scheduled enablement does not change;
- WM is never returned in status/settings APIs.

This is a bounded migration/fallback mechanism, not the preferred steady-state architecture.

## 11. Recu local archive repair

`RecuLocalArchivesCollector` remains local-file-only.

It now explicitly understands common cache concepts including top-level `models` maps, `moments` arrays/maps and `video_meta` maps.

New diagnostics include:

- `parsed_records`;
- `matched_known`;
- `skipped_unknown`;
- `imported`;
- `errors`;
- per-file detail.

Known account metadata can receive:

- `recu_moments` count;
- `recu_recordings` count;
- compatible cached fields already recognized by the flexible parser.

Unknown archive rows remain enrichment-only and are skipped rather than exploding the candidate universe.

The collector cursor is v4-prefixed so cache files that v0.3 marked seen while importing zero are reparsed once after upgrade.

This source performs no Recu web request, login, browser launch, Cloudflare interaction or credential capture.

## 12. Recommendation eligibility / gender policy

The user explicitly stated women are not of interest and asked whether male/couples should be filtered.

v0.4 makes this a global recommendation eligibility rule rather than forcing the user to Ignore hundreds of known-ineligible accounts.

Default:

```json
"recommendation_filters": {
  "allowed_genders": ["m"],
  "include_unknown_gender": true,
  "include_couples": false
}
```

Rationale:

- known male: eligible;
- known female: excluded by default;
- generic couple `c`: excluded by default because it does not tell us whether the room is male/male versus mixed-gender;
- unknown gender: allowed by default because historical/local models often lack fresh affiliate metadata and should not be discarded merely because data is missing;
- trans (`t`) can be enabled explicitly from Setup if desired.

Browser **Setup** exposes Male / Trans / Female / Couples / Unknown toggles and applies them immediately.

`POST /api/settings` is intentionally narrow: it changes only recommendation filters, atomically rewrites active config while preserving unrelated keys/secrets, and never returns the WM ID.

## 13. Preference semantics change

`PREFERENCE_WEIGHTS["test"]` is now `0.00`.

Meaning:

- Test remains a valid workflow state and maps to its normal numeric CTBRec priority for eventual sync;
- Test does **not** mean the user likes the model;
- Test is therefore no longer a positive taste seed.

Positive taste seeds are Favorite / Likely Favorite / Continue. Unsorted/Test are uncertain. Low Priority/Ignore can contribute negative evidence where the scorer supports it.

## 14. Score semantics / explainability

The existing score remains an interpretable 0–100 **ranking index**, not a probability.

Lanes:

- preference/tag match: 45;
- neighbor graph: 25;
- Recu rank/momentum/clip: 15;
- context: 5;
- controlled exploration: 10.

A weakly evidenced candidate can still receive a baseline/exploration value, so the UI now labels:

- score `/100`;
- evidence level (`low` / `medium` / `high`);
- confidence separately;
- component chips;
- reasons.

`DiscoveryEngine.diagnostics()` exposes preference/gender/evidence counts and active filters so the user can see whether personalization inputs actually exist before judging recommendation quality.

## 15. Reviewer health probe fix

The field server returned HTTP 501 to `HEAD /` even though it was reachable (`CTBRecMobile/2.15.5`).

v0.4 probe behavior:

1. try bounded `HEAD /`;
2. if 405 or 501, try a bounded small `GET /`;
3. treat 401/403 as reachable + auth boundary;
4. do not label a healthy service offline merely because its handler does not implement HEAD.

## 16. User-facing configuration checker

New:

- `discovery/check_config.py`;
- `scripts/CHECK_CONFIG.bat`.

Purpose:

- parse active config;
- verify configured local file paths where appropriate;
- probe loopback Mobile Reviewer and Live Control ports/services;
- explain disabled/missing local inputs.

It intentionally does **not** run the Chaturbate collector, contact Recu or start scraping/network discovery. This is the appropriate user troubleshooting tool.

`scripts/VALIDATE.bat` remains a developer/release regression suite.

## 17. Current HTTP/UI surface

### GET

- `/api/health`
- `/api/recommendations?limit=N`
- `/api/continuations?limit=N`
- `/api/accounts`
- `/api/outbox`
- `/api/integration`
- `/api/collectors`
- `/api/diagnostics`
- `/api/settings`
- `/api/sync` (queues async sync)
- `/api/sync-status`

### POST

- `/api/evidence`
- `/api/feedback`
- `/api/undo`
- `/api/merge`
- `/api/continuation-decision`
- `/api/settings`
- `/api/collectors/run`

### Browser tabs

- Discover;
- Possible Returns;
- Sources;
- Setup;
- Status.

Sync now has visible busy feedback rather than looking inert.

## 18. Media UX request and current implementation boundary

The user explicitly requested each Discover recommendation to support:

1. live Chaturbate room playback using the exact proven Live Control methodology;
2. Recu Best Moments playback side-by-side when available, using the Rapid Sorter methodology.

### Live Control facts to preserve

Current Live Control handoff documents:

- server-side thumbnail endpoint for tracked models;
- user-triggered `POST /api/media/start {"name":"username"}` HLS resolution;
- direct CDN HLS first, same-origin proxy fallback;
- one active room at a time;
- muted start;
- no background stream-resolution loop;
- exact tracked-model authorization boundary.

v0.4 does **not** duplicate or weaken this stack. Discover cards surface tracked/online state and an Open Room action, and explicitly note where a playback hook exists. Actual inline media reuse remains next work because cross-service authentication and the tracked-only restriction need a deliberate local broker/shared-media design.

### Recu Best Moments facts to preserve

Rapid Sorter already owns local Best Moments files under:

`runtime/recu_best_moments/`

Local playback can bypass live Recu entirely. The next Discovery media release should first index those existing files lazily and serve them with Range support rather than launching a new Recu fetcher.

Do not auto-load video for every card. Media should remain click-to-load/on-demand to protect bandwidth/CPU.

## 19. Account continuity status

Continuity remains separate from affinity.

Supported evidence includes:

- official/public redirect evidence;
- username morphology;
- timing/schedule/language/tag/public-profile continuity;
- explicit imported links.

Rejected pairs persist. Merge remains explicit user action.

Discovery does not compute face embeddings or automated facial biometric identity matching.

CamGirlFinder may remain an external/manual/reference source, but Discovery must not silently turn third-party face matches into automatic identity merges.

## 20. Persistence and upgrade safety

Default DB: `state/discovery.sqlite3`, WAL mode.

Schema v2 tables include:

- identities;
- accounts;
- evidence;
- feedback;
- identity_links;
- source_state;
- action_outbox.

v0.4 requires no destructive migration.

PATCH must never contain/overwrite:

- `discovery_config.json`;
- `state/` database/WAL/shm;
- logs;
- runtime/private files;
- local Recu session/browser material;
- WM credentials;
- user caches/data.

## 21. Current mutation boundary

Feedback and Undo may enqueue `preference_sync_request` intents with:

- requested label;
- numeric priority;
- account references;
- `target=verified_mobile_reviewer_live_bridge_adapter`;
- `execute=false`.

There is no consumer in v0.4.

Do not add a consumer until the exact current bridge API/source is available and validated. The future consumer must preserve target-installation fingerprint isolation, be idempotent and never fall back to normal direct `models.json` mutation.

## 22. v0.4 files added/changed

Key changed files:

- `discovery/__init__.py`
- `discovery/config.py`
- `discovery/importers.py`
- `discovery/collectors.py`
- `discovery/models.py`
- `discovery/scoring.py`
- `discovery/engine.py`
- `discovery/integration.py`
- `discovery/server.py`
- `discovery/check_config.py` (new)
- `discovery_config.example.json`
- `static/index.html`
- `static/app.js`
- `static/styles.css`
- `scripts/CHECK_CONFIG.bat` (new)
- `scripts/VALIDATE.bat`
- `tests/test_core.py`
- `validation/validate_v040.py` (new)
- `validation/validate_package.py`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/INTEGRATION_CONTRACT.md`
- current handoff/test report.

## 23. Automated validation baseline for v0.4

As of the release candidate working tree:

- **23 unit/regression tests** pass;
- Python source/tests/validation scripts compile;
- `node --check static/app.js` passes;
- `validation/validate_v040.py` passes and covers:
  - real `catalog` map/list cache shape;
  - nonblocking App construction;
  - local fake Live Control reuse;
  - preference import from local Live Control payload;
  - local Recu cache enrichment;
  - hard female/couple filtering;
  - real HTTP smoke for health/diagnostics/settings/recommendations.

Release-candidate packaging gates completed:

- package safety validator: PASS;
- v0.3→v0.4 candidate PATCH overlay preserved seeded active config/DB/private runtime byte-for-byte;
- clean candidate FULL SOURCE extraction reran compile, 23 tests, JavaScript syntax, v0.4 integration harness and package validation: PASS.

Final archive bytes are rebuilt only after the current handoff/test report are frozen. The release procedure then re-runs exact final PATCH overlay, clean final FULL validation, ZIP integrity and SHA-256 generation. `TEST_REPORT_v0_4_0.txt` + `SHA256SUMS_CTBR_DISCOVERY_v0_4_0.txt` are the authoritative record of those final-byte gates.

## 24. User field test after installing v0.4

Recommended sequence:

1. Stop v0.3 Discovery.
2. Overlay v0.4 PATCH; preserve current active config/state.
3. Optional: run `scripts\CHECK_CONFIG.bat`.
4. Run `START_DISCOVERY.bat`.
5. Confirm the `listening on http://127.0.0.1:8793` line appears immediately rather than after source work.
6. Open Setup:
   - Male checked;
   - Female unchecked;
   - Couples unchecked;
   - Unknown checked;
   - adjust if desired.
7. Open Sources and confirm `live control models` is enabled/loopback-only and eventually `ok` if Live Control is running.
8. Direct Chaturbate should stay scheduled-disabled; if a WM remains configured, a one-shot may be run manually when desired.
9. Click Sync and confirm visible Syncing/completion feedback.
10. Status/source details should show the Mobile Reviewer catalog importing real rows on first v0.4 sync; later unchanged sync should report `unchanged` rather than reparsing.
11. Recu local source should report nonzero `parsed_records` and, if account names overlap, nonzero `matched_known`; `imported` may still be lower depending on actual useful metadata.
12. Discover readiness should show a meaningful account count and positive preference seed count if Live Control/catalog state exposes priorities.
13. Known female cards should disappear under the default filter; unknown-gender cards may remain.
14. Do not judge final recommendation quality yet if neighbor/Recu-rank evidence counts are still zero; that means those upstream candidate signals are not yet connected.

If preference seeds remain unexpectedly zero, capture `/api/diagnostics`, `/api/collectors` details for `live_control_models`, and a safe sample shape of `/api/models` with private fields redacted. Do not ask for `controller_config.json` by default.

## 25. Exact next engineering priorities

### P0 — media evidence on Discover cards

1. Index Rapid Sorter's local `runtime/recu_best_moments/` media + sidecars.
2. Add Discovery Range-capable local media endpoint for already-local Best Moments.
3. Add a card-level Best Moments player that loads only on tap.
4. Reuse Live Control room thumbnail/player through an authenticated local broker/shared service **without weakening tracked-model authorization**.
5. If new/untracked discovery candidates require live preview, extend Live Control deliberately with a discovery-safe preview contract backed by its accepted full affiliate snapshot; do not bypass its current tracked-model guard.

### P0 — complete global affiliate deduplication

When exact current Live Control source is available, add a sanitized complete accepted affiliate-snapshot endpoint/local snapshot file. Discovery should consume that and retire normal independent affiliate polling.

### P1 — true personalized candidate signals

1. Produce/consume neighbor graph evidence automatically from approved/public sources.
2. Connect Recu bookmark rank/momentum/clip-velocity exports so the 15% lane becomes real.
3. Add source-ablation/performance diagnostics: which sources cause promoted candidates to become Favorite/Likely Favorite/Continue.

### P1 — session opportunity layer

Separate queue/score for promising model sessions using self-normalized activity anomalies and clip freshness. Do not let raw viewers/tips increase model affinity.

### P2 — verified CTBRec preference sync

Only after exact current bridge implementation can be inspected/tested: consume inert outbox through exact-target verified mutation path with idempotency/undo semantics.

## 26. Non-negotiable safety/architecture invariants

- Sorting/review must never wait for Discovery.
- No recurring whole-library scan from Discovery.
- No direct `models.json` mutation.
- No guessed CTBRec bridge operations.
- No face recognition/biometric identity engine.
- No Recu verification/access-control bypass.
- No competing Recu session/browser owner.
- No hidden high-frequency external polling.
- External source failures must fail closed and preserve cached UI.
- Media is lazy/on-demand; no background stream-resolution loop.
- Preference score is explainable and must not be mislabeled as a probability.
- Known hard eligibility constraints should filter candidates instead of being learned through thousands of avoidable negative labels.
- Every patch upgrade must preserve user state byte-for-byte unless an explicit migration is required and separately validated.
