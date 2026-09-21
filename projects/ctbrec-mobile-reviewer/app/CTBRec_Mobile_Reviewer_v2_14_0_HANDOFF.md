# CTBRec Mobile Reviewer v2.14.0 — Complete Development Handoff

**Last updated:** 2026-09-11  
**Current baseline:** v2.13.3 field-regression recovery + v2.14.0 incremental Recu/CDP/direct-disk preview  
**Current status:** **FINAL / GREEN FOR AUTOMATED RELEASE GATES; REAL RECU/CDP + IPHONE FIELD CONFIRMATION STILL REQUIRED.**

This section is authoritative. If historical appended sections conflict with it, **v2.14.0 wins**.

## NON-NEGOTIABLE USER DELIVERY RULE

With **EVERY assistant development message, whether final, partial, diagnostic, WIP, or blocked**, provide all three current downloadable artifacts:
1. complete current **FULL SOURCE ZIP**;
2. current safe **PATCH/UPDATE ZIP**;
3. complete current **HANDOFF Markdown** containing implemented + planned work, validation/failures, known issues, architecture/context, safety rules and exact next steps.

If work is unfinished, artifacts must still be produced and clearly labeled WIP/interim. Never silently substitute an older version while describing newer code.

---

# 0. USER REQUEST THAT CREATED v2.14.0

The user asked for two changes:

1. **Recu/Recurbate scanning memory + faster background retrieval**
   - Maintain running per-model session memory so repeated Recu scans only inspect sessions/kinks newer than the last successful scan.
   - Recu pages are ordered newest on page 1 to oldest on the last page; as soon as a kink scan reaches a previously-seen session/date, stop that kink's pagination.
   - Reuse the architecture from the user's separate Recu clip-scraper work: preferred 12-way concurrent requests through verified Chrome's CDP `Network.loadNetworkResource` with credentials, rate-limit backoff, explicit re-authentication on verification/login/401/403/bogus 404/no clips, and a lightweight verified Chrome navigation fallback that blocks images/video/fonts and stops loading as soon as clip DOM is available.

2. **Desktop-local video preview**
   - When previewing from the desktop that physically hosts the files, avoid phone-oriented compatibility streaming/transcoding and play the original file as directly as browser security allows.

---

# 1. v2.14.0 IMPLEMENTED CHANGES

## 1.1 Durable incremental Recu scan memory

File: `ctbrec_mosaic_sort_lite.py`

`RecuModelData` now includes `scan_memory` and `recu_data_to_payload` / `recu_data_from_payload` persist it in the existing `mosaic_lite_recu_cache.json` model entry.

Memory schema:
- `schema_version: 1`
- `known_sessions`: model-wide `{video_id: recording_end_iso}` map
- `kink_sessions`: per-kink `{slug: {video_id: recording_end_iso}}`
- `last_scan_at`
- `last_pages_fetched`
- `last_new_sessions`
- `last_stop_boundaries`
- `concurrency`

Migration is automatic. A pre-v2.14 cached model has no explicit `scan_memory`; `_recu_memory_from_cached()` derives stable boundaries from already cached `RecuMoment` rows, grouped by kink slug.

Why memory is per-kink as well as model-wide: using only a global session boundary could incorrectly truncate a newly discovered kink slug because another kink had already seen the same historical session. A new slug therefore receives a complete first scan; subsequent runs stop against that slug's own prior session/date set.

## 1.2 Newest-to-oldest early stop

Helper: `_take_new_recu_moments_until_known()`.

For each ordered kink listing page:
1. parse rows in DOM order;
2. retain rows until the first row whose `video_id` OR exact recording-end timestamp was present in that kink's **previous scan memory**;
3. stop processing older rows/pages for that kink at that boundary;
4. merge only the genuinely new moments into the already cached historical moments.

Important: newly found sessions in the current refresh are **not** used as the stop boundary for another kink during the same run. Only memory from the previous successful scan defines the stop point.

`force=True`/manual Refresh now means “check for new metadata immediately”; it does not discard running scan memory or force a full historical rewalk.

## 1.3 Concurrent listing-page retrieval

`start_recu_for_queue()` supplies the new batch-capable fetch callbacks when the dedicated verified Chrome browser is running.

Default `recu.concurrent_requests = 12` (bounded to 1–24).

Hot-path behavior:
- Recu kink index is loaded once.
- First listing page for every discovered kink slug is submitted through the batch fetcher, up to 12 simultaneously.
- Most routine refreshes should therefore finish after this first batch because page 1 reaches prior session memory.
- If a kink contains enough new sessions to require older pages, pagination links are fetched in ordered batches of up to 12.
- Responses may complete concurrently, but they are **processed in page-number order** so the “stop at first known historical boundary” rule remains deterministic.

## 1.4 Preferred verified-Chrome `Network.loadNetworkResource` path

File: `ctbrec_mobile_server.py`, class `RecuBrowserBridge`.

New preferred method: `fetch_html_background()`.

It:
- attaches to the existing dedicated verified Recu Chrome profile through CDP;
- enables `Network`;
- reads the current root frame ID;
- invokes experimental CDP `Network.loadNetworkResource` with `options = {disableCache: true, includeCredentials: true}`;
- reads the returned stream using `IO.read`;
- does **not** visibly navigate the browser tab for each background page.

`fetch_many_background()` runs up to 12 of those resource requests concurrently using independent CDP websocket connections.

No credentials/cookies are written to logs.

## 1.5 Rate-limit handling

On HTTP 429:
- honor numeric `Retry-After` where available;
- otherwise exponential backoff from `recu.rate_limit_base_seconds` (default 1.5 s), bounded to 60 s per request;
- use a bridge-wide shared `_rate_limit_until` so the concurrent request pool does not immediately re-hammer the site while one worker is backing off;
- default `network_retry_attempts = 4`.

## 1.6 Re-authentication detection

New exception: `RecuReauthRequired` with `reauth_required = True`.

The Chrome-backed path deliberately fails closed and asks for user re-authentication when it sees:
- HTTP 401;
- HTTP 403;
- unexpected/bogus HTTP 404 for a requested Recu page;
- Cloudflare/browser challenge markers (`cf-chl-`, “verify you are human”, “just a moment”, etc.);
- login redirect/page indicators;
- empty HTML;
- a page expected to contain clip listings that unexpectedly contains no `video-thumb` / video-play links.

`start_recu_for_queue()` converts this to queue status `needs_verification` rather than `error` or `empty`.

Phone behavior:
- Recu panel already changes its title/button to “Recu verification needed” / “Settings”;
- v2.14 additionally shows a one-time toast when the current queue transitions into `needs_verification`, directing the user to Mobile Settings → Recu → verify/capture session.

If the dedicated Chrome browser is closed but the captured cookie session still exists, Reviewer preserves the pre-v2.14 captured-cookie HTTP fallback rather than automatically opening a visible browser window. If that legacy path returns obvious 401/403/verification/login errors, `start_recu_for_queue()` also converts them to `needs_verification`.

## 1.7 Lightweight verified Chrome navigation fallback

If Chrome refuses/does not support `Network.loadNetworkResource`, `RecuBrowserBridge.fetch_html()` / `fetch_many()` switch to `fetch_html_navigation()`.

Fallback behavior:
- uses the already verified Chrome tab/profile;
- enables `Network.setBlockedURLs` for common image/video/font extensions;
- `Page.navigate` to the desired URL;
- polls lightweight DOM snapshots;
- as soon as required clip listings are present (or a non-listing page reaches usable ready state), calls `Page.stopLoading`;
- remains sequential because visible-tab navigation is stateful and reliability is preferred over trying to navigate one tab concurrently.

This implements the same fast-path/fallback model requested from the separate Recu clip-scraper project.

## 1.8 Recu timing resolution uses the same preferred fetch path

`resolve_recu_moment_timings()` now accepts `fetch_html_fn`.

When a current/upcoming chunk requires an unresolved Recu recording duration, Mobile Reviewer uses the same Chrome/CDP-backed fetcher when available instead of reverting to an unrelated raw request path.

## 1.9 Desktop-local original-file preview

Files: `ctbrec_mobile_server.py`, `static/app.js`.

Each preview item now exposes `local_disk_url`, which is the existing authenticated `/api/video/...?...direct=1` Range endpoint pointed at the **original recording**.

On a desktop browser whose hostname is exactly `localhost`, `127.0.0.1`, or `::1`:
- preview tries `local_disk_url` first;
- server calls `send_range_file()` directly against the source path;
- no ffmpeg process is opened;
- no remux/transcode is performed;
- browser seeks by HTTP Range directly into the original file.

Why this is not a literal `file://C:/...` URL: normal HTTP/PWA pages are intentionally prohibited by browser security from arbitrarily opening local file URLs. Loopback HTTP Range is the safe browser-compatible equivalent while still reading the original file bytes directly from disk.

If desktop Chrome cannot decode the original container, the client automatically falls back once to the existing fast-remux URL. iPhone/Tailscale behavior is unchanged.

## 1.10 PWA cache/version

Current shell cache: `ctbrec-shell-v2140`.
Current static URLs: `?v=2140`.
After patching, fully kill/reopen the iPhone Home Screen app once.

---

# 2. VALIDATION COMPLETED FOR v2.14.0

New regression: `validation/validate_v2_14_recu_incremental.py`.

It creates a synthetic Recu model with ordered pages and verifies:
1. first scan discovers two sessions and persists them;
2. persisted `scan_memory` contains stable session boundaries;
3. second scan presents one new session followed by a known historical session on page 1;
4. exactly the new session is merged;
5. the scan stops at the prior boundary and **does not request page 2**;
6. requested batch concurrency is 12;
7. challenge HTML, login HTML, and a normal-looking but unexpectedly empty listing all raise `RecuReauthRequired`;
8. source contains `Network.loadNetworkResource` + `includeCredentials`, navigation fallback blocking + `Page.stopLoading`, and direct-disk preview wiring.

Existing suites also pass after the change:
- `validate_v2_13_core.py`
- `validate_v2_13_browser_dom.py`
- `validate_v2_13_ffmpeg_batch.py`
- `validate_v2_13_nsfw_batch.py`
- `validate_v2_13_http.py`
- `validate_v2_13_1_recovery.py` (cache-version assertion updated to v2140)
- `validate_v2_13_2_catalog_abort.py`
- `validate_v2_13_3_field_regressions.py`

`mobile_self_test.py` is extended with markers for incremental Recu memory, concurrent verified-Chrome loading, navigation fallback, and desktop direct-disk preview. In the Linux build container the complete self-test cannot pass because the container Python lacks the Windows installation's NudeNet package; this is an environment limitation, not a source failure. The user's Windows Python 3.10 installation previously reported NudeNet 3.4.2 installed.

No live Recu request was made from the build environment, so `Network.loadNetworkResource` compatibility with the user's exact installed Chrome build remains a **field-validation item**. The fallback architecture exists specifically so refusal/unsupported behavior does not make the feature unusable.

Release packaging hardening also passed:
- final PATCH contains none of `mobile_config.json`, roots, durable queues, Recu session/profile/cache, catalog/READY/background/NSFW state, logs or model binaries;
- a seeded v2.13.3 install retained byte-identical sentinel runtime state after the v2.14.0 PATCH overlay;
- the final FULL archive was extracted into a clean directory and the complete source regression suite was run against the extracted archive contents;
- the FULL archive's fresh-install config is shipped with blank PIN/secret so first start generates installation-local values.

---

# 3. SAFETY / BEHAVIORAL INVARIANTS THAT MUST REMAIN

Do not regress any of these:
- Sorting UI must remain available while Recu/background/NSFW work runs.
- Recu is enrichment-only and must never block mosaic selection/submission.
- Existing Recu cache remains usable if a live refresh fails.
- Recu credentials/session cookies must never be logged or placed in diagnostic bundles.
- CTBRec native mutations still go only through the identity-verified live bridge; no `models.json` write fallback.
- Existing v2.13.3 settings optimistic-lock guard and corruption repair must remain.
- Catalog/model-tab requests continue using response sequencing **without AbortController**.
- “N mosaics ready” means N durable-ready; `open_fast_initial_chunks` only limits synchronous warm validation.
- Exact-signature READY snapshot hydration must occur before rebuilt chunks are repersisted.
- Background whole-library generation keeps bounded retry/skip and self-restart behavior.
- Precise Review frame-cut outputs verify before originals move to `MARKED_FOR_DELETION`.
- Frame-cut work is idle-only and prioritized above whole-library mosaics/NSFW background work.
- Desktop direct playback may bypass ffmpeg only for preview; it must never mutate the source file.

---

# 4. CURRENT CONFIGURATION ADDITIONS

Under `mobile_config.json` → `recu` (defaults supplied automatically if absent):
- `concurrent_requests`: 12 (bounded 1–24)
- `network_retry_attempts`: 4
- `rate_limit_base_seconds`: 1.5
- `navigation_fallback`: true

No user migration/edit is required. The safe PATCH must never overwrite an existing `mobile_config.json`.

---

# 5. KNOWN LIMITATIONS / FIELD TESTS STILL NEEDED

1. **Live Chrome CDP:** confirm the user's Chrome accepts `Network.loadNetworkResource` with a frame target and returns a stream. If not, verify the navigation fallback activates and remains fast.
2. **Real Recu ordering:** implementation assumes each kink page is newest-to-oldest as specified by the user. If Recu changes ordering, early-stop logic must be revisited.
3. **Retrospective site edits:** by design, incremental scanning can miss a kink that Recu adds retrospectively to a session older than the remembered boundary. This follows the user's requested stop-at-known-date behavior. A future optional “Full historical rescan” command could bypass memory if desired; it is not currently exposed.
4. **Desktop direct container support:** MP4/MOV should normally play directly. Some TS/MKV codecs/containers may not be browser-decodable; automatic remux fallback handles that case.
5. **Recu performer page:** duration harvesting still fetches the performer page each refresh. Individual video pages remain lazy and bounded to current/upcoming chunks.

---

# 6. EXACT NEXT STEPS FOR A FUTURE AGENT

If user reports Recu issues:
1. collect `mobile_reviewer.log` and the existing diagnostics ZIP; do **not** request/upload `mobile_recu_session.json` or `recu_browser_profile` unless absolutely necessary because they contain authentication material;
2. inspect queue task result for `new_sessions`, `pages_fetched`, `stop_boundaries`;
3. inspect `mosaic_lite_recu_cache.json` only if the user is comfortable sharing model/session metadata; confirm the model's `scan_memory` structure;
4. determine whether the path used background CDP or navigation fallback from logs/progress before changing scraping logic;
5. preserve the explicit re-auth fail-closed conditions instead of interpreting auth failures as an empty model.

If user asks for a full rescan feature:
- add an explicit opt-in flag that ignores `previous_kink_sessions` for that invocation only;
- do not overload ordinary Refresh, because ordinary Refresh is now intentionally incremental.

If user reports desktop preview latency:
- first confirm they are opening Reviewer at `http://127.0.0.1:8787` or `localhost` on the host PC;
- confirm `local_disk_url` is selected and no ffmpeg preview process starts;
- if the original container is unsupported, the automatic remux fallback is expected.

---

# 7. RELEASE / PACKAGING REQUIREMENTS

Every future release response must include:
- complete full-source ZIP;
- safe patch ZIP excluding runtime/private state;
- current handoff Markdown.

The PATCH must exclude at minimum:
- `mobile_config.json`
- `recording_roots.txt`
- `mobile_action_queue.json`
- `mobile_recu_session.json`
- `recu_browser_profile/`
- `mosaic_lite_recu_cache.json`
- catalog/READY/background/NSFW state caches
- logs
- Reviewer sidecar metadata
- generated mosaics
- bundled NudeNet binary/model when it is unchanged

Current release files should be named:
- `CTBRec_Mobile_Reviewer_v2_14_0_FULL_SOURCE.zip`
- `CTBRec_Mobile_Reviewer_v2_14_0_PATCH.zip`
- `CTBRec_Mobile_Reviewer_v2_14_0_HANDOFF.md`

---

# 8. HISTORICAL v2.13.3 HANDOFF (PRESERVED VERBATIM BELOW)

The following older handoff is retained so a future agent has the complete regression history and architectural detail. Where it conflicts with sections 0–7 above, v2.14.0 is authoritative.

# CTBRec Mobile Reviewer v2.13.3 — Complete Development Handoff

**Last updated:** 2026-09-11  
**Current baseline:** v2.13.2 conservative mobile transport + v2.13.3 field-regression recovery  
**Current status:** **FINAL / GREEN FOR AUTOMATED RELEASE GATES; REAL IPHONE FIELD CONFIRMATION STILL REQUIRED.** The real field diagnostics were used to identify the regressions below. Source/JS validation, real-FFmpeg regression tests, catalog/browser transport tests, clean full-source extraction, patch-content isolation, and seeded v2.13.2→v2.13.3 runtime-preservation overlay checks all pass.

This section is authoritative. If older appended handoff sections conflict with it, **v2.13.3 wins**.

## NON-NEGOTIABLE USER DELIVERY RULE

With **EVERY assistant development message, whether final, partial, diagnostic, WIP, or blocked**, provide all three current downloadable artifacts:
1. complete current **FULL SOURCE ZIP**;
2. current safe **PATCH/UPDATE ZIP**;
3. complete current **HANDOFF Markdown** containing implemented + planned work, validation/failures, known issues, architecture/context, safety rules and exact next steps.

If work is unfinished, artifacts must still be produced and clearly labeled WIP/interim. Never silently substitute an older version while describing newer code.

---

# 0. WHY v2.13.3 EXISTS — REAL FIELD DIAGNOSTICS

The user installed v2.13.2 and supplied:
- `mobile_reviewer.log`;
- `CTBRec_Mobile_Reviewer_Diagnostics_20260910_184813.zip`;
- the complete `install_mobile_reviewer.bat` self-test output.

Reported field symptoms:
1. installer ended with `v2.13 stale-tab request cancellation: MISSING` even though the app otherwise mostly ran;
2. a catalog tile could say e.g. **15 mosaics ready**, but opening that model said **3 mosaics ready, the rest generating**;
3. entering/leaving a model could make the model tile become **not ready** even though it was ready immediately before the click;
4. Recu did not work even in Originals, and user wanted it to work in Review too;
5. EZ Sort populated, then later became **0 models**, apparently around mobile model-controller/settings activity;
6. background mosaic generation logged a `NameError: name 'normalized' is not defined` and skipped affected models after bounded retries.

## What the diagnostic bundle proved

The underlying library/catalog was healthy:
- 6,166 Original folders in cached catalog;
- 1,488 Review folders;
- a local `/api/catalog` probe returned 3,643 aggregated Original models in roughly 250–311 ms;
- Review probe returned 498 models;
- EZ Sort probe returned exactly 0.

Therefore the blank/zero states were not caused by the user's recording drives disappearing.

The runtime config had a distinctive corruption fingerprint matching an **uninitialized Settings form POST**:
- Original mosaic 15 sec / 1 column / 160 px / 1 max frame / use-existing false;
- Review same minima;
- `mosaic_settings.recu_enabled=true` but new `recu.enabled=false` and Recu debug port 1024;
- background mosaics disabled / upcoming 0;
- whole-library generation disabled;
- Non-NSFW disabled and its controls at minima;
- model admin disabled + auto-discovery false;
- live bridge disabled + auto-discovery false;
- frame-cut idle delay 0.

Log chronology showed EZ Sort had 25 models, then settings/controller/Recu activity occurred, `POST /api/settings` occurred, and later EZ Sort returned zero. The v2.13.x client had unrelated actions that called `saveSettings()` as a side effect, so an incompletely initialized Settings page could serialize its HTML default/min values across unrelated configuration sections.

The log also captured the exact batching crash:
`ctbrec_mosaic_sort_lite.py ... grouped.setdefault(normalized(row[1].path), []).append(row)` -> `NameError: name 'normalized' is not defined`.

---

# 1. v2.13.3 IMPLEMENTED FIXES

## 1.1 Installer/self-test false failure

`mobile_self_test.py` previously required the old v2.13 catalog AbortController behavior. v2.13.2 deliberately removed that behavior, so the test was stale and caused `install_mobile_reviewer.bat` to report setup failure.

v2.13.3 now checks the actual safe architecture:
- `modelLoadSeq` response sequencing exists;
- catalog calls use `noAbort: true`;
- no `modelLoadController` exists.

Expected label: `v2.13.3 stale-tab response sequencing without catalog aborts: present`.

## 1.2 READY count no longer collapses to the warm-open limit

`speed_mode.open_fast_initial_chunks` exists to minimize phone-tap latency. In v2.13.0-v2.13.2 it accidentally became a hard cap on the chunks loaded from the durable ready snapshot.

Correct v2.13.3 semantics:
- if the catalog says 15 ready, **all 15 durable-ready chunks enter the queue**;
- only the first N (default 3) are synchronously validated before returning the tap;
- later chunks are lazily validated when they become current;
- stale/missing later outputs are regenerated individually rather than redefining readiness up front.

Regression test: 10 durable-ready rows -> 10 queued/ready, exactly 3 synchronous `_chunk_mosaic_ready` checks.

## 1.3 Opening a model cannot erase existing READY state

Root bug: priority/background chunk builders rebuilt metadata and could persist a new ready snapshot **before restoring the old mosaic output paths**. This became especially destructive after the corrupted Settings POST set `use_existing_mosaics=false`.

New helper: `_hydrate_chunks_from_ready_snapshot(mode, model_name, chunks)`.

Safety rule:
- only an **exact chunk signature** may inherit old mosaic paths;
- changed chunk signatures never inherit paths;
- normal layout/file readiness validation still occurs before actual use.

The helper runs after Original/Review metadata build in both normal library and priority/model-open paths, before snapshots are repersisted. An explicit regression test confirms a rebuilt exact-signature Original chunk retains a ready snapshot.

## 1.4 v2.13.x Settings corruption is repaired conservatively

New helpers:
- `_looks_like_v213_settings_form_corruption(raw)`;
- `_repair_v213_settings_form_corruption(config)`.

Detection intentionally requires the distinctive multi-section field fingerprint from the real diagnostic bundle. It is **not** a generic “user picked unusual settings” reset.

On detection at startup:
1. the raw existing config is copied locally to `mobile_config.pre_v2_13_3_settings_repair_YYYYMMDD_HHMMSS.json`;
2. sane defaults are restored for the affected sections;
3. Recu becomes enabled on port 9223;
4. existing Recu Chrome/base URL fields are preserved where possible;
5. background mosaic/library/NSFW defaults are restored;
6. model-admin and live-bridge auto-discovery are re-enabled while explicit path values are preserved;
7. frame-cut idle threshold returns to 10 minutes;
8. Original/Review mosaic defaults return to 300s/180s, 3 columns, 480 px, 240/300 frames, use-existing true.

**Security note:** the local pre-repair backup is the original full config and may contain PIN/secret values. It is for local recovery and should not be uploaded casually.

## 1.5 Settings writes now fail closed

Server adds `settings_revision_token()`, based on editable config fields excluding `access_pin` and `secret_key`.

`GET /api/settings` returns `settings_revision`.

`POST /api/settings` requires the exact current revision:
- no revision -> reject before mutation;
- stale revision -> reject before mutation.

Client tracks `settingsLoaded` + `settingsRevision`. Save refuses to run until Settings has successfully loaded.

Most importantly, these actions no longer implicitly save the Settings form:
- Run Non-NSFW now;
- Run whole-library mosaics now;
- Launch Recu browser.

They may refresh Settings **after** the action, but do not POST it first.

This protects even a stale cached PWA: old clients lacking `settings_revision` are rejected server-side instead of corrupting configuration.

## 1.6 Recu works in Originals **and Review**

Server queue Recu state, launch/refresh, submit/back progression, cheap status payload, and full current payload now treat `mode in {original, review}` as eligible.

Client Recu panel, in-place marker updates, kink summary and post-session refresh now support Review as well.

The Recu matching functions operate on shared chunk attributes (`start`, `end`, `files`, per-file start/end), which Review chunks provide.

The user's immediate Recu failure was also caused by the corrupted config contradiction (`legacy recu_enabled=true`, new `recu.enabled=false`). The settings repair restores the authoritative new Recu config to enabled.

## 1.7 EZ Sort is now durable and independent from controller enablement

Prior behavior:
- legacy EZ membership was read from read-only CTBRec `models.json` notes;
- `MobileModelAdmin.paths()` returned no paths whenever `model_admin.enabled=false`;
- therefore the corrupted Settings POST disabling model admin could make EZ Sort instantly become 0;
- a controller restart/path transient could also make legacy-only membership disappear.

v2.13.3:
- read-only legacy metadata discovery may run with `include_when_disabled=True`;
- controller `enabled` governs the controller UI/mutations, not compatibility filter metadata;
- discovered legacy EZ names are imported into Reviewer-owned `mobile_model_metadata.json` under `easy_sort_imported`;
- once imported, legacy path/controller transients cannot erase those names;
- explicit Reviewer `easy_sort_overrides` still wins, including opt-out;
- cache signature is recomputed after an import write so the next query does not unnecessarily reparse all legacy files.

No native `models.json` mutation was added.

## 1.8 Original batched FFmpeg NameError fixed

In `ctbrec_mosaic_sort_lite.generate_mosaic`:
- bad: `normalized(row[1].path)`;
- correct: `normalized_absolute(row[1].path)`.

A new regression test creates a real synthetic video with FFmpeg and runs the **complete** Original `generate_mosaic()` path, not merely the lower-level batch helper. This catches the exact field failure that earlier batching tests missed.

## 1.9 PWA/version

- server version: `CTBRecMobile/2.13.3`;
- service worker: `ctbrec-shell-v2133`;
- index static asset query version: `?v=2133`.

User must fully kill/reopen the iPhone Home Screen PWA once after patching.

---

# 2. VALIDATION ADDED/UPDATED FOR v2.13.3

New: `validation/validate_v2_13_3_field_regressions.py`.

Covers:
- exact real-world Settings corruption fingerprint;
- near-miss config is not auto-repaired;
- repaired values restore Recu/background/admin/live/mosaic sane defaults;
- settings revision guard rejects missing/stale writes before mutation;
- exact-signature ready-path hydration;
- ready snapshot remains ready after rebuilt metadata is repersisted;
- Review queues enter Recu path;
- real FFmpeg complete Original mosaic generation succeeds without the `normalized` NameError.

Updated `validate_v2_13_core.py`:
- EZ Sort legacy membership becomes Reviewer-durable;
- disabling controller does not erase imported EZ membership;
- open-fast 10-ready case queues 10 while validating only 3.

Release regression set (all passed in the final package build):
- `validate_v2_13_2_catalog_abort.py`;
- `validate_v2_13_1_recovery.py`;
- `validate_v2_13_http.py`;
- `validate_v2_13_browser_dom.py`;
- `validate_v2_13_ffmpeg_batch.py`;
- `validate_v2_13_nsfw_batch.py`;
- all Python compilation;
- JS syntax checks.

---

# 3. SAFETY / ARCHITECTURE INVARIANTS — DO NOT REGRESS

1. **User can always sort:** Recu, model controls, NSFW, background generation and PC file work must not modal-block or rebuild the mosaic surface unnecessarily.
2. **Catalog networking remains conservative:** model-list GET has no AbortController; stale responses are suppressed by generation/sequence checks; Python server stays HTTP/1.0/plain JSON unless a real-device-tested transport change proves safe.
3. **No silent `models.json` writes:** legacy file is read-only compatibility metadata. Live CTBRec native mutations go only through the identity-verified bridge.
4. **Destructive media safety:** generated frame-cut outputs verify before originals move to `MARKED_FOR_DELETION`; durable PC queue and undo invariants remain.
5. **Background liveness:** real PCQ/current-phone work may pause low-priority generation; ghost historical queues may not; bad models use bounded retry/skip rather than wedging the pass.
6. **Frame-cut priority:** idle-eligible precise cuts outrank whole-library mosaics/NSFW, but active phone/PC work can preempt them safely.
7. **Batched FFmpeg:** retain bounded process batching for Original, Review and NSFW paths; missing outputs fall back narrowly rather than rerunning everything.
8. **READY semantics:** ready count is durable work availability, not “how many items were synchronously validated during the phone tap.”
9. **Settings fail closed:** never auto-POST the whole Settings form as a prerequisite for an unrelated button.
10. **Every development response must ship full source + patch + handoff.**

---

# 4. EXACT NEXT STEPS FOR A FUTURE AGENT

Final packaging gates completed on 2026-09-11:
1. every validation listed above passed;
2. all Python files compiled and app/rapid/service-worker JavaScript syntax checks passed;
3. full source was packaged only after restoring sanitized runtime/template state;
4. patch was inspected and contains no active config, roots, action queue, catalog/ready state, Reviewer metadata, NSFW runtime state, or NudeNet model payload;
5. final full source retains the bundled NudeNet 640m model;
6. the full-source ZIP was extracted into a clean directory and every validation suite reran successfully against the extracted bytes;
7. a seeded v2.13.2 install with sentinel runtime files was overlaid with the patch and every sentinel remained byte-for-byte unchanged.

After user installs:
- confirm `install_mobile_reviewer.bat` no longer reports the stale-tab check missing;
- confirm startup logs the one-time settings repair on the affected install;
- confirm All Models and EZ Sort counts populate;
- confirm a tile reporting N ready still reports N after opening/back;
- confirm Recu panel works in both Original and Review;
- confirm background mosaic generation no longer emits `NameError: normalized`;
- if EZ Sort still differs from expected, collect a new diagnostics ZIP and inspect `mobile_model_metadata.json` plus discovered legacy `models.json` paths before touching networking.

---

# APPENDED PRIOR AUTHORITATIVE HANDOFF (v2.13.2 and earlier)

# CTBRec Mobile Reviewer v2.13.2 — Complete Development Handoff

**Last updated:** 2026-09-10  
**Current baseline:** v2.13.1 emergency transport rollback + v2.13.2 model-catalog AbortController removal/diagnostics  
**Current status:** **FINAL / GREEN for automated release packaging; real iPhone field confirmation still required.** All current automated validation, clean-archive revalidation, and seeded v2.13.1→v2.13.2 state-preservation overlay checks pass. v2.13.2 specifically addresses the field error `Model list temporarily unavailable: signal is aborted without reason` by removing browser abort signals from catalog/model-list requests entirely.

This is the authoritative current handoff. If an older appended section conflicts with this v2.13.2 section, **v2.13.2 wins**.

## NON-NEGOTIABLE DELIVERY RULE FROM USER

With **EVERY assistant development message, whether work is complete, partial, diagnostic, WIP, or blocked**, output all three downloadable artifacts:
1. complete current **FULL SOURCE ZIP**;
2. current safe **PATCH/UPDATE ZIP**;
3. complete current **HANDOFF Markdown** with all implemented changes, planned work, validation results/failures, known issues, architecture/context, safety invariants and exact next steps another AI agent needs to resume.

Do not wait for a final release to provide these. If unfinished, label them WIP/interim clearly.

---

# 1. FIELD FAILURE THAT CREATED v2.13.2

After installing v2.13.1, user reported the phone showing:

`Model list temporarily unavailable: signal is aborted without reason`

No models appeared.

The user asked:
- where the app is looking for the model list;
- why this is happening;
- which files they can upload to diagnose it;
- why the app started breaking after a previously working version.

## Root-cause boundary established from source comparison

The previously field-working v2.12 model-list path was extremely simple:

- `loadModels()` performed one plain browser `fetch()` via the simple `api()` wrapper;
- there was **no AbortController** in the model-list path;
- no per-tab request abort; no browser-enforced catalog timeout.

v2.13 introduced a request-sequencing/latency optimization that added `AbortController` to model-list requests. v2.13.1 correctly reverted the risky HTTP/1.1/gzip/global Authorization transport changes, but **left this catalog AbortController architecture in place**:

1. every `loadModels()` aborted the previous model-list controller when a new load began;
2. the generic API wrapper also created another AbortController for a 30-second request timeout;
3. Safari/iOS can expose an aborted Fetch signal as a DOM error whose message is `signal is aborted without reason`.

The exact field abort could have been either a superseded catalog request or the 30-second timeout because both flowed through the same abort mechanism. Without the user's runtime trace it is not valid to claim which of those two fired. What **is** established is that this message originates from the client-side abort architecture added after the stable version, not from a missing model folder.

## Why things suddenly broke after a good version

v2.13 attempted to solve real lag/crash problems by changing too many layers near the phone request path:
- v2.13.0 changed HTTP/1.0 -> HTTP/1.1, enabled Python gzip, added global Authorization header, and added model request cancellation/retries;
- real iPhone/Tailscale then showed `Failed to fetch` and blank model lists;
- v2.13.1 reverted HTTP/1.1/gzip/global Authorization and decoupled bootstrap/catalog, but retained the new AbortController catalog cancellation logic;
- the next real-phone failure exposed that remaining regression as `signal is aborted without reason`.

So the recent breakage is not because the underlying library suddenly changed. It is a regression in the optimization work around the mobile request lifecycle. The correct strategy is now conservative: preserve safe performance wins but move catalog loading back toward the known-working plain-fetch behavior.

---

# 2. WHERE THE PHONE MODEL LIST ACTUALLY COMES FROM

The phone does **not** recursively scan the user's ~30 TB library every time All Models/Review/EZ Sort is opened.

Current source path:

1. `ctbrec_mobile_server.py` resolves the configured roots file (normally `recording_roots.txt`) from `mobile_config.json`.
2. A disk catalog scan periodically builds model-folder rows from those roots and persists them to `mobile_catalog_cache.json`.
3. At server startup, that cache is loaded into `STATE.catalog` in memory.
4. Phone tabs call `GET /api/catalog?...`.
5. `STATE.catalog_models(...)` filters/aggregates the **in-memory catalog** by:
   - mode (`original`, `review`, `deletion`, cleanup special path);
   - selected drives;
   - hidden/ignored/EZ Sort Reviewer metadata;
   - read-only legacy models.json metadata for compatibility;
   - cached ready-mosaic counts for the model-list hint.
6. The JSON list is sent to the phone.

Therefore a phone-side `signal is aborted...` error means the Fetch path failed/cancelled before a usable `/api/catalog` response reached the UI. It does **not** by itself mean `recording_roots.txt` or the model folders are empty.

---

# 3. v2.13.2 IMPLEMENTED FIXES

## A. Remove AbortController entirely from model-list/catalog GETs

File: `static/app.js`

- `loadModels()` no longer creates `state.modelLoadController`.
- it no longer calls `.abort()` when switching tabs/drives.
- it no longer passes `signal:` to `/api/catalog`.
- catalog uses `api(..., { timeoutMs: 0, maxAttempts: 1, noAbort: true })`.
- for this path the underlying `fetch()` therefore receives **no AbortSignal at all**.
- there is no 30-second browser self-abort on catalog requests.

This is intentionally closer to the v2.12 field-working behavior.

## B. Preserve stale-tab safety without request cancellation

The useful v2.13 stale-response fix remains, but it is now entirely logical rather than transport-level:

- every model load increments `state.modelLoadSeq`;
- each response captures its mode/drives `modelRequestKey`;
- if a response/error returns after the user changed mode/drives, it is ignored;
- a slow Review response therefore cannot overwrite a later EZ Sort view even though the browser was allowed to finish the old network request.

This avoids the old stale-tab bug without giving Safari a self-abort path.

## C. Generic API timeout errors are normalized

For non-catalog endpoints that still benefit from timeouts:
- the generic API helper may still use an internal controller;
- a timeout is tracked explicitly with `timedOut=true`;
- if it fires, the raw DOM abort wording is replaced with `Request timed out after N seconds.`;
- generic GET retries continue only for retryable network/status classes.

So user-visible `signal is aborted without reason` should not leak from normal timeout handling either.

## D. Catalog request timing/error instrumentation

File: `ctbrec_mobile_server.py`

`GET /api/catalog` now:
- measures server-side catalog-build time;
- returns `request_ms` in the response;
- records `STATE.last_catalog_request` with timestamp, mode/filter/drives, model count, elapsed milliseconds and success/failure;
- writes a log line if a catalog request takes >=500 ms;
- logs traceback + timing if catalog generation raises.

This distinguishes future problems:
- server-side catalog calculation slow;
- catalog calculation fast but phone/Tailscale transport slow;
- auth failure;
- app-side stale request/error.

## E. New diagnostics endpoint

Authenticated/local endpoint: `GET /api/diagnostics/status`

Returns:
- server version;
- catalog row counts by mode;
- current catalog status;
- last catalog request timing/error;
- roots file path;
- number of configured roots and number currently available;
- log/catalog-cache/ready-index runtime paths.

## F. One-click diagnostics collector

New files:
- `collect_mobile_diagnostics.py`
- `collect_mobile_diagnostics.bat`

The BAT is intended for the user's installed Reviewer folder. It creates:

`CTBRec_Mobile_Reviewer_Diagnostics_YYYYMMDD_HHMMSS.zip`

The collector includes, when present:
- `mobile_reviewer.log`;
- `mobile_catalog_cache.json`;
- `mobile_ready_work_index.json`;
- `mobile_library_mosaic_state.json`;
- `mobile_non_nsfw_state.json`;
- `mobile_non_nsfw_index.json`;
- `mobile_action_queue.json`;
- `mobile_hidden_models.json`;
- `mobile_model_metadata.json`;
- `recording_roots.txt`;
- a **redacted** mobile config;
- code checksums for server/PWA/core bridge files;
- local HTTP timing probes for auth, diagnostics status, Original catalog, Review catalog and EZ Sort catalog;
- Windows `netstat :8787`, Python task list, Tailscale status and Tailscale Serve status when available.

Privacy/security behavior:
- intentionally excludes Recu session/browser-profile data;
- recursively redacts config keys containing PIN/secret/token/cookie/password/credential/authorization;
- does **not** pretend model names or local paths are anonymous: runtime logs/catalog/state can still contain them. User should know this before sharing.
- individual runtime files >80 MiB are summarized rather than copied to prevent giant support bundles.

## G. PWA cache/version

- `Handler.server_version = CTBRecMobile/2.13.2`
- shell cache: `ctbrec-shell-v2132`
- app/rapid/styles asset query version: `2132`
- user must fully terminate/reopen the iPhone Home Screen app after applying patch.

---

# 4. VALIDATION COMPLETED FOR v2.13.2

All current validation programs pass in the development tree.

## Static/syntax
- `node --check static/app.js` PASS.
- `python -m py_compile` for server, diagnostics collector and validation code PASS.
- static invariant confirms the `loadModels()` block contains **no `signal:`** and uses `timeoutMs: 0`, `noAbort: true`.

## Phone-sized headless browser test

`validation/validate_v2_13_browser_dom.py` PASS:
- repeated bootstrap `TypeError("Failed to fetch")` does not block model list;
- first catalog failure auto-recovers without PIN;
- mocked catalog fetch records that **no AbortController signal was supplied**;
- deliberately slow prior Review response cannot overwrite later EZ Sort;
- 4,100 logical models are retained;
- progressive DOM remains bounded;
- frame selection still updates in place without rebuilding mosaic/file DOM.

## Real Python HTTP test

`validation/validate_v2_13_http.py` starts the current server in a temporary installation and confirms:
- HTTP/1.0/plain JSON remains;
- explicit paired-session recovery endpoint still works;
- versioned app asset streams with complete Content-Length;
- catalog endpoint returns successfully and includes `request_ms`;
- diagnostics status endpoint returns CTBRecMobile/2.13.2 plus catalog metrics.

## Existing v2.13 performance regression tests remain green
- model-admin filter cache;
- catalog view cache;
- lightweight queue status;
- open-fast initial 3-mosaic validation window;
- Original mosaic batched FFmpeg;
- Review mosaic batched FFmpeg;
- NSFW batch frame extraction + targeted fallback;
- NSFW cleanup mosaic batch extraction.

## RELEASE PACKAGE GATE + FIELD VALIDATION

Clean package validation is complete:
- final FULL_SOURCE extracted to a fresh directory; sanitized blank PIN/secret and empty action queue confirmed;
- final PATCH inventory contains none of the protected runtime/private files;
- Node/Python syntax and all current validation programs pass from the clean extracted FULL_SOURCE bytes;
- a seeded v2.13.1 installation with sentinel config, roots, action queue, catalog/ready/background/NSFW state, sidecars, log and Recu state/profile was overlaid with the PATCH and every protected sentinel remained byte-for-byte identical.

Real iPhone field validation is still required. Do not claim the physical phone issue is conclusively resolved until the user applies v2.13.2 and tests it.

---

# 5. EXACT FILES TO REQUEST IF THE USER STILL HAS A FAILURE

Preferred: ask the user to run `collect_mobile_diagnostics.bat` from the installed Reviewer folder while the server is running and upload the generated diagnostics ZIP.

If they cannot run it, request these manually:

1. `mobile_reviewer.log` — most important server/runtime log.
2. `mobile_catalog_cache.json` — proves whether the server actually has catalog rows/models cached.
3. `mobile_ready_work_index.json` — ready-mosaic hints/state.
4. `mobile_library_mosaic_state.json` — background library cursor/status.
5. `mobile_action_queue.json` — queued decisions/cuts/moves; useful for background contention.
6. `mobile_hidden_models.json` and `mobile_model_metadata.json` — EZ Sort/hidden/ignore filtering inputs.
7. `recording_roots.txt` — configured roots actually being cataloged.
8. **Prefer a redacted** `mobile_config.json`; never ask the user to expose access PIN/secret/cookies unnecessarily.
9. screenshot of the exact phone error and which tab was selected.
10. if possible, PC-local browser result for `http://127.0.0.1:8787` at the same moment.

Do **not** ask for `mobile_recu_session.json` or the Recu browser profile for this model-list issue; they contain unrelated session material.

Interpretation guide:
- local PC UI loads models + phone fails -> Tailscale/PWA/client transport problem;
- local PC UI also fails -> server/catalog/filter/lock problem;
- `mobile_catalog_cache.json` has thousands of rows but `/api/catalog` local probe fails -> server filtering/serialization bug;
- local diagnostics say `/api/catalog` is fast but phone remains slow -> network/PWA rendering path;
- `catalog_counts.original=0` -> investigate roots/catalog scan rather than phone request code.

---

# 6. SAFE PERFORMANCE FEATURES THAT MUST REMAIN

Do not regress these while stabilizing phone transport:
- 120-card progressive model rendering (+120 sentinel growth) instead of creating ~4,100 cards at once;
- request-sequence stale-response protection;
- bounded client model-view cache;
- server catalog and ready-count caches;
- fast model-open with only a small initial ready-mosaic validation window (default 3);
- lightweight current queue/status polling;
- streamed static/mosaic media rather than `read_bytes()` whole-file allocations;
- lazy image loading and explicit image-src release to reduce Safari decoded-bitmap pressure;
- hidden-PWA polling backoff;
- batched FFmpeg exact-frame extraction for Original mosaics, Review mosaics, NSFW sampling and NSFW cleanup mosaics (default batch size 6);
- targeted individual frame fallback when a batch output is missing.

Do not reintroduce browser AbortController on `/api/catalog` merely to reduce wasted requests. Request sequence checks are the current stable race-control mechanism.

---

# 7. PREVIOUS FEATURE/SAFETY INVARIANTS TO PRESERVE

## v2.12 precise Review frame cuts
- individual mosaic frame/timestamp boundary decisions;
- selection lasts until next unselected boundary;
- adjacent same-destination ranges merged;
- contiguous kept ranges can cross source segments;
- durable idle-only precise cut jobs outrank background mosaic/NSFW once idle-eligible;
- generated replacements must all verify before originals move to MARKED_FOR_DELETION;
- Back/cancel must safely stop/revert and only remove job-owned outputs.

## v2.11.3 whole-library liveness
- old phone queue/prefetch sessions cannot pin background generation;
- only active phone queue gets interactive priority;
- bounded per-model retries then skip/quarantine;
- frame extraction timeouts;
- whole-library worker self-restarts after unexpected failure/backoff;
- real PCQ instructions still correctly outrank background work.

## Sorting UX invariant
Ready mosaic sorting must remain interactive even when:
- Recu refreshes;
- model admin/live controls refresh;
- PCQ/background status changes;
- NSFW scan runs;
- whole-library mosaics run;
- frame-cut jobs are queued/waiting.

Background panels must not rebuild/reflow the mosaic surface under the user's finger.

## Live CTBRec bridge safety
- live/native mutations go only through the identity-verified JVM bridge;
- verify exact installation immediately before mutation;
- never silently mutate `models.json` as fallback;
- Reviewer-only Easy Sort/Hidden/alias/ignore state stays in sidecars;
- current persistent native Ignore limitation remains documented (live REMOVE + Reviewer ignore/deletion behavior).

---

# 8. PACKAGING RULES

PATCH must not include active/private runtime state. Exclude at minimum:
- `mobile_config.json`;
- `recording_roots.txt`;
- `mobile_action_queue.json`;
- Recu session/profile;
- catalog/ready/background/NSFW caches/state;
- generated mosaics/manifests;
- logs;
- Reviewer metadata sidecars;
- user/bundled NudeNet model unless a model update specifically requires it.

FULL SOURCE:
- may include sanitized template-like `mobile_config.json`, empty documented `recording_roots.txt`, and empty action queue for fresh install;
- must not include test-generated PIN/secret;
- must not include real user runtime state/logs/sessions;
- should include current validation, changelog, update guide, diagnostics collector and this handoff;
- bundled NudeNet model should remain included if full-package convention requires it.

Every release/handoff response must provide the three required artifacts even if status is WIP.

---

# 9. EXACT NEXT STEPS

1. User applies `CTBRec_Mobile_Reviewer_v2_13_2_PATCH.zip` over the existing v2.13.1/current installation.
2. Restart PC server.
3. Fully terminate and reopen the iPhone Home Screen PWA once so shell `v2132` loads.
4. Verify All Models populates; rapidly switch All Models -> Review -> EZ Sort -> Hidden -> All Models; open a model and sort.
5. If any model-list failure remains, user double-clicks `collect_mobile_diagnostics.bat` in the installed Reviewer folder while the server is running and uploads the generated `CTBRec_Mobile_Reviewer_Diagnostics_*.zip`.
6. Use its local `/api/catalog` timings and catalog counts to decide:
   - local catalog fast + phone fails => PWA/Tailscale path;
   - local catalog slow/fails => server catalog/filter/lock path;
   - catalog counts zero => roots/catalog scan issue.
7. Only after field stability, continue optional performance work such as IndexedDB last-good catalog, fully recycled card virtualization, or measured model-admin filter-token optimization.

Do not re-enable HTTP/1.1, Python gzip, global auth headers, or catalog AbortController without explicit real-device field validation.

---

# 10. KEY CURRENT FILES

- `ctbrec_mobile_server.py` — HTTP/auth/catalog/scheduler/action server + v2.13.2 catalog diagnostics.
- `static/app.js` — PWA request/catalog/tab/sorting logic; **catalog path intentionally has no AbortController**.
- `static/rapid.js` — rapid sorter behavior.
- `static/service-worker.js` — PWA cache shell v2132.
- `collect_mobile_diagnostics.py/.bat` — current support-bundle path.
- `ctbrec_mosaic_sort_lite.py` — Original mosaics/Recu/batched extraction.
- `ctbrec_review_folder_sort_lite.py` — Review mosaics/frame-cut media pipeline.
- `ctbrec_nsfw_cleanup.py` — batched NSFW sampling/cleanup.
- `ctbrec_mobile_model_admin.py` — sidecars + legacy read-only models.json compatibility.
- `ctbrec_live_bridge_client.py` — native running-instance bridge.
- `mobile_catalog_cache.json` — runtime disk catalog (not shipped in patch).
- `mobile_reviewer.log` — runtime diagnostics (not shipped in patch).
- `SELF_PROMPT_v2_13_PERFORMANCE_RELIABILITY.md` — original broad performance audit prompt.

---

# Appendix A — v2.13.1 handoff preserved verbatim for history

# CTBRec Mobile Reviewer v2.13.1 — Complete Development Handoff

**Last updated:** 2026-09-10  
**Current baseline:** v2.13.0 performance release + v2.13.1 emergency iPhone/Tailscale transport recovery  
**Current status:** **FINAL / GREEN for release packaging** — implementation PASS, deterministic regression validation PASS, seeded v2.13.0→v2.13.1 state-preservation overlay PASS, and clean candidate FULL_SOURCE extraction/revalidation PASS. Real iPhone/Tailscale field validation is still required.

This file is the authoritative continuation context. If anything in the appended historical v2.13.0 handoff conflicts with this v2.13.1 section, **this section wins**.

## Non-negotiable user deliverable requirement

With **EVERY assistant development message, complete or incomplete**, provide all three:
1. complete current **FULL SOURCE ZIP**;
2. current safe **PATCH/UPDATE ZIP**;
3. complete current **HANDOFF Markdown** containing implemented changes, planned work, validation status/failures, architecture, safety invariants, exact next steps, and all context another AI needs to resume.

Do not wait for a release to be final before producing those three artifacts. If unfinished, label the handoff/artifacts clearly as WIP/interim.

---

## 1. Immediate regression that created v2.13.1

After installing v2.13.0 the user reported the phone UI showing:

`PC temporarily unreachable — retrying automatically. Failed to fetch`

and **no models appeared at all**. The user was understandably angry because v2.13.0 was intended to fix phone crashes/lag and instead introduced a startup/model-list regression.

### What v2.13.0 changed too aggressively

v2.13.0 simultaneously changed three transport/auth behaviors:
- server protocol from the previously stable HTTP/1.0 behavior to `HTTP/1.1`;
- Python-layer gzip compression for larger JSON responses;
- a persistent paired-device token sent as `Authorization: Bearer ...` on **every** API request.

The deterministic v2.13.0 tests covered HTTP errors, stale response races and a synthetic 4,100-model DOM, but they did **not** reproduce the exact iOS standalone-PWA + Tailscale Serve transport path. The exact one of those three changes responsible for the real `Failed to fetch` cannot be proven without a packet/server trace from the user's machine, so v2.13.1 intentionally removes the whole risky transport bundle rather than guessing.

### v2.13.1 corrective architecture

Implemented in `ctbrec_mobile_server.py` and `static/app.js`:

1. **Restore HTTP/1.0 compatibility**
   - `Handler.protocol_version = "HTTP/1.0"`.
   - This matches the previously field-working transport.

2. **Remove Python-layer API gzip**
   - `send_json()` sends plain UTF-8 JSON with an exact `Content-Length`.
   - The old `speed_mode.json_gzip_threshold_bytes` config key may remain in existing configs for backwards compatibility, but v2.13.1 ignores it. Do not re-enable it without real iOS/Tailscale validation.

3. **Remove global Authorization header**
   - Normal API fetches no longer attach `Authorization`.
   - Cookie/Tailscale auth is again the ordinary request path.

4. **Preserve lost-cookie recovery without global headers**
   - The installation-bound paired token from v2.13 remains (`HMAC(secret_key, "ctbrec-mobile-device-v1")`).
   - Startup first performs normal `GET /api/auth`.
   - If that says unauthenticated and the PWA still has a stored paired token, it sends that token exactly once in the JSON body of `POST /api/auth/recover`.
   - A valid token causes the server to reissue the normal HttpOnly `ctbrec_auth` cookie.
   - Normal API/media requests then use the cookie.
   - Application mutations are still never replayed automatically.

5. **Bootstrap no longer gates the model list**
   - v2.13.0 did `auth -> bootstrap -> loadModels`; any bootstrap transport failure jumped to the outer catch and never called the catalog.
   - v2.13.1 makes bootstrap/status and catalog loading independent.
   - If bootstrap fails, `/api/catalog` is still attempted immediately. With no drive picker metadata yet, an empty drive filter safely means all drives.
   - Bootstrap retries separately in the background.

6. **Model-list reconnect is self-healing**
   - Catalog GET has bounded timeout/retry.
   - `TypeError: Failed to fetch`, timeout/AbortError, and retryable HTTP status are retried for GET only.
   - If an active same-tab model list already exists, a refresh failure leaves that last-good list visible and shows reconnect status.
   - A genuinely new uncached tab still clears the prior tab immediately so stale models are never misrepresented as belonging to the selected tab.
   - A failed catalog schedules automatic foreground retry rather than staying blank indefinitely.

7. **PWA cache bumped**
   - shell: `ctbrec-shell-v2131`
   - asset query version: `2131`
   - user must fully terminate/reopen the iPhone Home Screen app once after overlay.

---

## 2. Performance improvements from v2.13.0 that remain enabled

Do **not** throw away the useful v2.13 work while fixing transport:
- request sequence + AbortController prevents a late prior-tab response from overwriting the selected tab;
- client catalog cache bounded to 8 views;
- progressive rendering: 120 model cards initially, +120 as the sentinel approaches;
- 4,100 logical models remain searchable while DOM stays bounded;
- server `catalog_view_cache`, `ready_count_cache`, and model-admin filter caches;
- fast model-open path validates only a tiny warm ready window (default 3 chunks) and lets background priority workers repair/build the rest;
- lightweight `/api/queues/<id>/status` polling avoids repeatedly rebuilding mosaic geometry;
- validated mosaic layout/JPEG stat cache;
- static/mosaic file streaming avoids full `read_bytes()` RAM copies;
- lazy image loading and explicit old image `src` release reduce iOS decoded-bitmap pressure;
- hidden-PWA background polling backs off;
- exact-frame FFmpeg extraction batching across Original mosaics, Review mosaics, Non-NSFW sampling, and Non-NSFW cleanup mosaics;
- bounded batch size default 6 with targeted individual fallback for missing frames.

---

## 3. Earlier functionality/safety that MUST remain intact

### v2.12 precise Review frame cuts
- per-frame timestamp-boundary selection;
- selected interval runs until the next unselected boundary;
- adjacent same-destination kept intervals are merged;
- contiguous kept ranges may cross CTBRec segment files;
- durable idle-only cut queue outranks background mosaics/NSFW once idle-eligible;
- all replacement clips are verified before original source segments move to `MARKED_FOR_DELETION`;
- failure/cancel/Back must not strand originals or ambiguous outputs.

### v2.11.3 unattended background liveness
- stale/ghost phone leases do not pause whole-library mosaic generation;
- only currently visible queue gets phone-prefetch priority;
- model mosaic failures retry boundedly then skip/quarantine;
- FFmpeg frame extraction has hard timeouts;
- whole-library worker self-recovers from unexpected worker-level failure after backoff.

### Live CTBRec bridge
- native actions go through the identity-verified running JVM bridge, normally port 8791;
- verify exact CTBRec installation immediately before mutation;
- never silently fall back to editing `models.json`;
- Reviewer sidecars retain Easy Sort/Hide/alias/ignore metadata;
- native persistent IGNORE still is not exposed by the bridge; current documented live REMOVE + Reviewer-ignore/deletion behavior remains.

### UX priority invariant
A ready mosaic sorting surface must remain usable regardless of Recu, model controls, PC queue status refresh, NSFW scanning, whole-library generation, background cuts, or background metadata work. Dynamic panels must not resize/move mosaic controls under the user's finger.

---

## 4. v2.13.1 validation completed

### Syntax
- `node --check static/app.js` PASS.
- all project/validation Python files compile PASS.

### Actual Python HTTP transport test
`validation/validate_v2_13_http.py` launches the current server code and confirms:
- `/api/bootstrap` responds via HTTP/1.0;
- even with `Accept-Encoding: gzip`, server does not apply Python-layer gzip;
- plain JSON parses;
- `/api/auth/recover` validates the paired token and returns a Set-Cookie auth cookie;
- versioned app asset streams with complete Content-Length;
- current client source does not contain the old global Authorization-header behavior.

### Headless phone-sized browser regression
`validation/validate_v2_13_browser_dom.py` deliberately makes `/api/bootstrap` throw the exact browser-level `TypeError("Failed to fetch")` repeatedly and also makes the first catalog fetch fail. It verifies:
- bootstrap transport failure does **not** block/blank the model list;
- catalog retry recovers without showing PIN;
- slow Review response cannot overwrite subsequently selected EZ Sort;
- 4,100 model logical count is retained;
- DOM remains bounded;
- precise frame-selection tap updates in place without rebuilding mosaic/file DOM.

### Existing v2.13 performance tests rerun
- paired-device token derivation/invalidation PASS;
- model-admin filter cache PASS;
- server catalog view cache PASS;
- lightweight queue status avoids mosaic geometry PASS;
- open-fast touches only 3 of 10 ready mosaics PASS;
- Original 4 frames -> 1 FFmpeg process PASS;
- Review 4 frames -> 1 FFmpeg process PASS;
- NSFW detection batching + targeted fallback PASS;
- NSFW cleanup mosaic 4 tiles -> 1 FFmpeg process PASS.

The real iPhone/Tailscale environment still requires field validation. Do not claim the exact physical transport cause was conclusively identified; claim that the regression-causing transport bundle was rolled back and the startup/catalog dependency bug was concretely fixed.

### Release-package gate
- Candidate FULL_SOURCE ZIP extracted into a fresh directory PASS.
- Sanitized fresh-install `mobile_config.json` confirmed empty PIN/secret before validation.
- `mobile_action_queue.json` confirmed empty.
- Candidate PATCH inventory confirmed no protected runtime/private state or NudeNet binary.
- Current application + validation Python compiled from the clean extracted archive.
- `app.js`, `rapid.js`, and `service-worker.js` passed Node syntax checks from the clean extracted archive.
- All six current validation programs passed from the clean extracted archive.

---

## 5. Packaging/state rules

### Every response
Always ship:
- `CTBRec_Mobile_Reviewer_v2_13_1_FULL_SOURCE.zip` (or current later version);
- `CTBRec_Mobile_Reviewer_v2_13_1_PATCH.zip`;
- `CTBRec_Mobile_Reviewer_v2_13_1_HANDOFF.md`.

### PATCH must NEVER include active runtime/private state
At minimum exclude:
- `mobile_config.json`;
- `recording_roots.txt`;
- `mobile_action_queue.json`;
- Recu cookies/session/browser profile;
- catalog/ready/background/index caches;
- generated mosaics/manifests;
- logs;
- Reviewer sidecar metadata;
- bundled/user NudeNet model (not needed for this patch).

The full-source ZIP may contain only sanitized fresh-install versions of `mobile_config.json`, `recording_roots.txt`, and the action queue. It must never contain test-generated PINs/secrets or user state.

---

## 6. Exact next field-test steps

1. Stop Mobile Reviewer.
2. Overlay the v2.13.1 PATCH over the existing v2.13.0 installation.
3. Restart PC server.
4. Fully terminate/reopen iPhone PWA once.
5. Verify All Models appears.
6. Rapidly switch All Models -> Review -> EZ Sort -> Hidden -> All Models.
7. Open a large model and begin sorting; first ready mosaic should appear without waiting for unrelated background work.
8. Temporarily interrupt/recover phone connectivity; the app should reconnect without treating generic fetch trouble as PIN/auth loss.
9. If failure remains, compare:
   - PC-local dashboard `http://127.0.0.1:8787`;
   - iPhone/Tailscale PWA.
   If PC local works but iPhone fails, capture `mobile_reviewer.log` around the iPhone request and Tailscale Serve status. If PC local also fails, focus on server/catalog exception instead of PWA transport.

---

## 7. Planned optimization work after this emergency fix

Only continue performance work after v2.13.1 field stability is confirmed. Candidate work, in priority order:
1. add per-endpoint rolling latency/timeout metrics visible in Settings/diagnostics;
2. optionally persist one last-good catalog view in IndexedDB for instant cold-PWA paint during brief PC reconnects (avoid localStorage size issues);
3. true recycled/virtualized model-card list if progressive 120-card growth still becomes heavy after very long scrolling;
4. benchmark whether single-process multi-seek FFmpeg filtergraph architecture beats current bounded independent-input batching on the user's actual disks without reducing corrupt-file isolation;
5. add a one-click support snapshot that records client fetch failure stage (auth/bootstrap/catalog/open/current) without exposing secrets.

Do not re-enable HTTP/1.1, Python-layer gzip, or global custom auth headers merely for theoretical throughput. Any future transport optimization must be field-tested through the actual iOS standalone PWA + Tailscale Serve path before release.

---

## 8. Key files

- `ctbrec_mobile_server.py` — HTTP/auth/catalog/scheduler/queue server.
- `static/app.js` — main PWA state, API wrapper, auth recovery, catalog tabs, sorting.
- `static/rapid.js` — rapid sorter enhancements.
- `static/service-worker.js` — shell/offline cache.
- `ctbrec_mosaic_sort_lite.py` — Original mosaic generation/batched frame extraction/Recu integration.
- `ctbrec_review_folder_sort_lite.py` — Review mosaic/cut media logic.
- `ctbrec_nsfw_cleanup.py` — Non-NSFW sampling/cleanup mosaics and NudeNet inference.
- `ctbrec_mobile_model_admin.py` — Reviewer sidecars + read-only legacy models.json compatibility.
- `ctbrec_live_bridge_client.py` — verified live JVM bridge transport.
- `mobile_action_queue.json` — runtime only, preserve, never ship in patch.
- `SELF_PROMPT_v2_13_PERFORMANCE_RELIABILITY.md` — original whole-program optimization prompt.

---

# Appendix A — Prior v2.13.0 handoff preserved for full historical/architectural continuity

**IMPORTANT:** The prior handoff below describes the v2.13.0 transport experiment (HTTP/1.1/gzip/global Bearer token) as implemented. Those specific transport statements are **superseded by v2.13.1 above**. All unrelated architecture/history remains useful.

# CTBRec Mobile Reviewer v2.13.0 — Complete Development Handoff

**Last updated:** 2026-09-10  
**Current development baseline:** verified v2.12.0 source + v2.13.0 performance/reliability changes  
**Release status at this handoff:** implementation complete; seeded v2.12 -> v2.13 state-preservation overlay PASS; clean final archive extraction/validation is the only remaining release gate.

This file is intended to let another AI agent resume development from the exact current state without needing the prior chat.

---

## 1. Immediate user request that created v2.13

The user reported that the iPhone/PWA repeatedly appeared to crash and ask for the PIN again; switching among All Models / EZ Sort / other model tabs was extremely laggy and could continue showing the previous model list; tapping a model and getting to a sortable mosaic was too slow; and mosaic generation appeared to repeatedly start/stop FFmpeg for every image. The user explicitly asked for a whole-program investigation and optimization, not a one-off fix, including a self-authored technical prompt that should then be executed.

The resulting self-directed prompt is stored verbatim in:

`SELF_PROMPT_v2_13_PERFORMANCE_RELIABILITY.md`

That prompt remains part of the source tree and defines the complete v2.13 investigation/validation scope.

The user then added a persistent handoff requirement:

> With EVERY assistant message, complete or not, always provide (1) a full current source ZIP, (2) a patch/update ZIP, and (3) a complete up-to-date Markdown handoff containing every implemented/planned change and all context needed for another AI to resume.

Future agents must preserve that response/deliverable behavior.

---

## 2. Current release lineage and invariants that MUST NOT regress

### v2.12.0 — precise Review frame cuts
- Review mosaics support individual sampled-frame boundary selection.
- Consecutive same-destination selected intervals are merged.
- Kept ranges may span adjacent CTBRec source segments.
- Durable idle-only FFmpeg cut jobs outrank whole-library mosaic generation and Non-NSFW scanning once idle-eligible.
- Every replacement clip is verified before original Review recordings move to `MARKED_FOR_DELETION`.
- Back/Undo can cancel pending/running work and restore originals/remove only uniquely job-owned outputs.

### v2.11.3 — unattended background liveness
- Whole-library generator ignores expired/ghost phone queue leases.
- Only the currently visible active queue receives automatic prefetch priority.
- FFmpeg frame extraction has hard timeouts.
- Failing models have bounded retries then skip/quarantine so one model cannot pin the pass forever.
- Whole-library worker self-restarts after an unexpected worker-level failure/backoff.

### v2.11.x — live CTBRec bridge
- CTBRec-native actions use the existing identity-verified running JVM bridge (normally port 8791).
- The exact CTBRec installation identity is verified immediately before native mutation.
- Do not add a silent `models.json` write fallback.
- Reviewer-only Easy Sort/Hide/alias/ignore metadata uses sidecars.
- Existing bridge still does not expose a native persistent IGNORE command; Ignore uses the previously documented live REMOVE + Reviewer metadata/deletion behavior.

### UX invariant
A ready sorting surface must not wait for Recu, live model controls, whole-library mosaic generation, NSFW scanning, durable filesystem work, or background status refreshes. Phone interactions win.

---

## 3. v2.13 implemented changes

### A. Authentication / apparent PWA crash loop
Files: `static/app.js`, `ctbrec_mobile_server.py`

- `init()` now distinguishes genuine auth loss from transient bootstrap/catalog/network failure.
- A network/bootstrap failure keeps the app in the models UI, reports temporary PC unavailability, and retries automatically.
- Only 401/403 or explicit `authenticated:false` can route to the PIN view.
- Server exposes an installation-bound durable device token:
  - `device_token_value = HMAC(secret_key, "ctbrec-mobile-device-v1")`
  - separate HMAC context from the HttpOnly auth cookie.
- PWA stores the paired token in `localStorage` as `ctbrec_device_token` after authentication.
- API requests send it as `Authorization: Bearer <token>`.
- If the token is valid but the HttpOnly cookie was lost, `/api/auth` reissues the cookie.
- Rotating/changing the server `secret_key` invalidates both token and cookie.
- Idempotent GET requests have bounded retry/timeout behavior.
- Mutating requests remain **single-attempt**; do not auto-replay ambiguous destructive/native actions.

### B. Model tab race / stale list bug
File: `static/app.js`

- `state.modelLoadSeq` sequences model-list requests.
- `state.modelLoadController` aborts superseded catalog fetches.
- A response applies only when its sequence and model request key still match the active tab/drives.
- When switching to an uncached target tab, old tab rows are cleared immediately and the UI shows Loading instead of leaving stale models onscreen.
- Short-lived client cache enables fast return to recently viewed tab/drive combinations.
- Client model cache is bounded to **8** catalog views to control Safari memory.

### C. 4,100+ model rendering optimization
Files: `static/app.js`, `static/styles.css`, `static/rapid.js`

- Initial model DOM is capped to **120** cards.
- Another 120 are appended progressively as an IntersectionObserver sentinel approaches the viewport.
- Search/filtering still runs against the complete fetched catalog, so logical results remain correct.
- CSS uses mobile-friendly content/layout containment where applicable.
- Rapid-sort model-card enhancements now resolve progressively rendered cards by `data-model-name` rather than repeatedly rescanning the entire model array for each visible card.

### D. Server catalog / model-admin caching
Files: `ctbrec_mobile_server.py`, `ctbrec_mobile_model_admin.py`

- `catalog_view_cache` caches derived model rows by mode, drive scope, filter, catalog stamp, and model-admin signature with a short TTL.
- `ready_count_cache` calculates ready-chunk hints from ready-index metadata instead of validating every mosaic image for the list.
- `MobileModelAdmin` caches Hidden/Ignore/Easy Sort sets by stat signature of its sidecars and discovered legacy `models.json` files.
- Unchanged legacy files are not reparsed on each catalog request.
- Model-admin writes explicitly invalidate derived filter caches.
- Catalog scans/ready-index saves invalidate appropriate server-side derived caches.

### E. Faster model open / no synchronous model crawl
File: `ctbrec_mobile_server.py`

- `quick_load_queue()` now uses the durable ready index and validates only a tiny warm set before returning.
- Default `speed_mode.open_fast_initial_chunks = 3`, clamped to a small safe maximum.
- It sorts ready-index metadata first and touches only ready rows needed for the immediate first queue.
- If the ready index is absent/stale, the phone returns immediately with no-ready state and the existing priority model-preparation worker repairs/builds missing work asynchronously.
- `open_model_sort_first()` promotes the clicked model after the immediate response path and can suggest another currently-ready model without doing a synchronous model-folder crawl.

### F. Lightweight current-screen polling
Files: `ctbrec_mobile_server.py`, `static/app.js`

- New cheap `queue_status_payload()` powers `/api/queues/<id>/status`.
- It returns queue/recu/background/mosaic state without opening mosaic JPEGs or rebuilding exact layout geometry.
- The phone uses this during the roughly 2.2-second active-work refresh cycle.
- The full `/current` payload is fetched only when the current chunk/layout actually changes or a previously missing mosaic becomes ready.

### G. Mosaic layout geometry cache
File: `ctbrec_mobile_server.py`

- Exact embedded mosaic layout validation is cached in memory against output identity/mtime/layout signature.
- Repeated current/status-related calls no longer reopen the same JPEG merely to rediscover dimensions/layout.
- Existing exact hit maps remain authoritative; no guessed legacy tap map was introduced.

### H. Lower DOM churn during sorting
File: `static/app.js`

- Frame-cut tile selection changes only the touched tile CSS state, its file-row state, and summary/draft state.
- It does not rebuild the full mosaic or file list after every tap.
- Background/Recu updates remain in-place and do not rerender the sorting surface.

### I. Safari image-memory pressure mitigation
File: `static/app.js`

- Only the first mosaic part is eager/high-priority; later parts are lazy/async/low-priority.
- `releaseReviewDom()` explicitly removes mosaic image `src` values before dropping the review DOM.
- `renderMosaics()` now also detaches old image `src` values before replacing one chunk's mosaic with another. This is intended to reduce retained decoded-image surfaces in long iPhone sessions.

### J. Polling while app is hidden
File: `static/app.js`

- Background polling slows substantially while `document.hidden`.
- Foreground visibility immediately triggers a fresh background/current update.
- This avoids waking a suspended iOS PWA for useless status traffic.

### K. HTTP/server delivery optimization
File: `ctbrec_mobile_server.py`

- `Handler.protocol_version = "HTTP/1.1"`.
- JSON uses compact separators.
- Large JSON responses use low-level gzip (`compresslevel=1`) when the client advertises gzip; default threshold = 4096 bytes.
- Static/mosaic files stream from disk using `shutil.copyfileobj` rather than `read_bytes()` allocating the whole file first.
- Existing Windows/browser disconnect guard remains active for WinError 10053/BrokenPipe/reset behavior.

### L. Batched FFmpeg extraction — Original mosaics
File: `ctbrec_mosaic_sort_lite.py`

- Added `extract_frames_batch()`.
- Default `frame_extract_batch_size = 6`, clamp 1..12.
- Multiple exact independently-seeked timestamp inputs are emitted by one FFmpeg process to separate JPEGs.
- Batches are grouped by source file.
- Missing batch outputs fall back only for the missing timestamp via existing single-frame extraction.
- Existing hard timeout/cancellation remains.
- Mosaic composition happens afterward in the original visual plan order, so batching does not change tap/source ordering.

### M. Batched FFmpeg extraction — Review mosaics
File: `ctbrec_review_folder_sort_lite.py`

- Added `extract_review_frames_batch()` and targeted single-frame fallback.
- Default batch size 6, bounded.
- Exact continuous Review timeline/tap metadata remains unchanged.
- v2.12 frame-cut sidecar timestamps (`chunk_seconds`, interval end, source boundaries) remain intact.

### N. Batched FFmpeg extraction — Non-NSFW scanning and cleanup mosaics
File: `ctbrec_nsfw_cleanup.py`

This was the last unfinished item from the prior chat and is now implemented.

- Added `extract_frames_batch()` to the Non-NSFW worker.
- `_scan_one_file()` now extracts each NudeNet detection batch through bounded FFmpeg frame batches instead of one FFmpeg process per sample image.
- If a batch produces a missing JPEG, only that sample falls back to the old `extract_frame()` path.
- If even the fallback fails, the file remains `uncertain`, preserving the scanner's conservative safety behavior.
- `_compose_mosaic()` now batches candidate mosaic frame extraction by source within each bounded mosaic part.
- Missing cleanup-mosaic frames remain visible `FRAME ERROR` tiles and do not silently alter file/tile association.
- Added defaults:
  - `non_nsfw_cleanup.frame_extract_timeout_seconds = 45`
  - `non_nsfw_cleanup.frame_extract_batch_size = 6`
- `mobile_config.template.json` and server defaults/settings persistence know these values.

Important design detail: the batch command uses several independently seeked inputs inside **one FFmpeg process** rather than converting the sample grid to an approximate `fps`/select filter. This reduces process-start overhead while keeping each requested timestamp exact. It may still open the same source multiple times inside the process; that is an intentional robustness/exactness tradeoff.

---

## 4. Validation implemented and currently passing

Reproducible validation scripts are stored under `validation/`:

- `validate_v2_13_browser_dom.py`
- `validate_v2_13_core.py`
- `validate_v2_13_http.py`
- `validate_v2_13_ffmpeg_batch.py`
- `validate_v2_13_nsfw_batch.py`

Current results:

1. **PWA/browser DOM simulation**
   - 3 consecutive synthetic bootstrap 503 failures do not show PIN.
   - automatic recovery succeeds once server responds.
   - deliberately slow Review response cannot overwrite later Easy Sort response.
   - 4,100 logical models remain searchable/countable while DOM stays bounded.
   - paired-token code path present.
   - frame selection updates without rebuilding mosaic/file nodes.

2. **Core server/cache tests**
   - paired token is stable for same secret and changes with installation secret.
   - Easy Sort/Hidden/Ignore filter cache reuses unchanged legacy state and invalidates when source changes.
   - catalog view cache hits return equivalent rows and invalidate on admin/catalog change.
   - lightweight queue status does not touch `layout_parts()`.
   - open-fast validates exactly 3 ready chunks from a synthetic 10-row snapshot when configured for 3.

3. **HTTP tests**
   - HTTP/1.1 response.
   - gzip large JSON.
   - streamed static app.js response.

4. **FFmpeg process-count tests**
   - Original: 4 frame requests -> 1 FFmpeg process.
   - Review: 4 frame requests -> 1 FFmpeg process.
   - Non-NSFW detection: 3 timestamps -> one batch process; deliberate removal of one output causes one targeted single-frame fallback and still completes safely.
   - Non-NSFW cleanup mosaic: 4 tiles -> 1 FFmpeg process.

5. **Syntax**
   - all top-level Python modules compile.
   - app.js, rapid.js, service-worker.js pass `node --check`.

6. **NudeNet 640m model**
   - size: `103,538,690` bytes
   - SHA-256: `04fe3d77980780c1f8297dc6d7f942fd5b3abe6942a188f742a85241e4f634eb`

### Validation environment limitation
The Linux tool environment does not have the `nudenet` Python package installed, so `mobile_self_test.py` stops at that dependency check. This is not a source compile failure. The bundled ONNX file is hash-verified, the Non-NSFW module compiles, and its new extraction pipeline is tested with a deterministic fake detector plus real FFmpeg. On Windows, `install_mobile_reviewer.bat` installs `requirements.txt` before invoking `mobile_self_test.py`.

Direct browser navigation to localhost is also blocked by an administrator policy in this tool environment, so the PWA regression uses Playwright `set_content` with deterministic fetch mocks while executing the current real `app.js`/`rapid.js` source.

See `TEST_REPORT_v2_13_0.txt` for the release validation summary.

---

## 5. Current file map / architecture

### Phone/PWA
- `static/index.html` — main UI structure/tab controls/settings/review UI.
- `static/app.js` — primary PWA state, auth, model catalog, model open, review sorting, settings, polling.
- `static/rapid.js` — rapid/Tinder-like sort and offline pack extensions/overrides.
- `static/styles.css` — responsive/mobile layout and containment.
- `static/service-worker.js` — shell cache (`ctbrec-shell-v2130`) and offline mosaic cache behavior.
- `static/manifest.webmanifest` — PWA metadata.

### PC/server
- `ctbrec_mobile_server.py` — HTTP API, auth, queue coordination, catalog, open-fast, action queue, background scheduling, video endpoints, frame-cut execution, caches.
- `ctbrec_mosaic_sort_lite.py` — Original scanning/chunking/mosaic generation and exact Original hit maps.
- `ctbrec_review_folder_sort_lite.py` — Review chunking/mosaics and frame-cut timestamp metadata/media helpers.
- `ctbrec_nsfw_cleanup.py` — low-priority NudeNet worker, scan cache, cleanup mosaics.
- `ctbrec_mobile_model_admin.py` — Reviewer sidecars + read-only compatibility with legacy models.json + live model control resolution.
- `ctbrec_live_bridge_client.py` — identity-verified bridge transport/native actions.
- `ctbrec_keep_last.py` — Keep Last helper.

### Runtime/private files (DO NOT ship in patch; preserve on update)
Examples include:
- `mobile_config.json`
- `recording_roots.txt`
- `mobile_action_queue.json`
- `mobile_recu_session.json`
- `mobile_catalog_cache.json`
- ready/background/index caches
- Recu browser profile/session
- generated mosaics/manifests
- logs
- Reviewer sidecar metadata generated by the user's installation

The patch must contain code/templates/docs only. The full-source distribution may include a **sanitized default** `mobile_config.json`, an empty/comment-only `recording_roots.txt`, and an empty action queue for fresh-install usability, but never the user's active versions.

---

## 6. Configuration additions / defaults in v2.13

`speed_mode`:
- `open_fast_initial_chunks`: 3
- `json_gzip_threshold_bytes`: 4096

Original/Review mosaic settings:
- `frame_extract_timeout_seconds`: 45
- `frame_extract_batch_size`: 6

Non-NSFW cleanup:
- `frame_extract_timeout_seconds`: 45
- `frame_extract_batch_size`: 6

Batch sizes are bounded by code. Do not blindly raise them for throughput; phone responsiveness and drive contention matter more than maximizing FFmpeg parallel work.

---

## 7. Packaging requirements for this release and every future response

The user explicitly requires **all three on every assistant response**:

1. **FULL SOURCE ZIP** — complete current source/package so another chat can continue without missing files.
2. **PATCH ZIP** — safe overlay/update package from the user's current verified baseline; do not include active runtime state.
3. **CURRENT HANDOFF MARKDOWN** — this file (updated for every change, including unfinished work/plans/validation/known failures/exact next steps).

For v2.13.0 the intended names are:
- `CTBRec_Mobile_Reviewer_v2_13_0_FULL_SOURCE.zip`
- `CTBRec_Mobile_Reviewer_v2_13_0_PATCH.zip`
- `CTBRec_Mobile_Reviewer_v2_13_0_HANDOFF.md`

Also produce/use:
- `TEST_REPORT_v2_13_0.txt`
- `SHA256SUMS_v2_13_0.txt`

---

## 8. Known limitations / things not to misrepresent

- A deterministic browser simulation has validated the logic, but the user's real iPhone/Safari/Tailscale environment still needs field testing. Do not claim the memory/crash symptom is guaranteed eliminated until the user runs it.
- The durable paired token is deliberately tied to the existing server `secret_key`. If the user deletes/rotates that key or starts a fresh installation identity, a one-time re-pair/PIN is expected.
- FFmpeg batching reduces **process launches**, not necessarily all underlying file-open work; exact independent seeks are intentionally preserved.
- The existing live JVM bridge still has no native persistent IGNORE command.
- No new approximation or destructive fallback should be introduced just to gain speed.

---

## 9. Planned / exact next steps from this handoff

Release packaging progress already completed:
1. Patch assembled from code/static/template/docs/validation only.
2. Seeded v2.12 copy created with unique protected runtime-state sentinels.
3. Patch overlay PASS: every protected state/model file listed in the validation report remained byte-identical and v2.13 code landed.

Final release gate completed:
4. Candidate patch/full archives were rebuilt with the updated handoff/report.
5. Both candidate ZIPs were extracted into fresh directories. Patch inspection confirmed that protected active runtime state was absent.
6. All 13 Python files in the clean FULL_SOURCE extraction compiled; `app.js`, `rapid.js`, and `service-worker.js` passed `node --check`.
7. All five reproducible v2.13 validation programs passed when executed from the clean FULL_SOURCE extraction.
8. The release is FINAL/GREEN. `SHA256SUMS_v2_13_0.txt` is generated externally after the final archives are rebuilt with this marker.

The next development action should be **user field validation**, not speculative rewrites:
- install patch over v2.12;
- fully close/reopen iPhone PWA once;
- test switching Original -> Review -> Easy Sort -> Hidden rapidly;
- test returning between tabs after loading ~4,100 models;
- open a large model with some ready mosaics and confirm first mosaic appears quickly;
- leave background generation/NSFW running and confirm sorting remains responsive;
- observe whether PIN reappears after temporary phone/network/server interruptions;
- if Safari still terminates, collect `mobile_reviewer.log`, approximate number/size of mosaic parts visible before termination, iPhone free memory/storage context if available, and whether crash happens on model list or mosaic view.

Potential future optimization only if field evidence shows need:
- more aggressive virtualization/recycling of model cards instead of progressive 120-card growth;
- a persistent IndexedDB model-catalog cache if server round trips remain painful over remote Tailscale;
- instrumented server timing headers/per-endpoint rolling latency metrics;
- optional FFmpeg batch-size UI control (currently config/default driven);
- more sophisticated single-input filtergraph extraction if benchmarks prove the independent-input batch still causes too much disk overhead, but only if exact timestamp semantics and corrupt-file isolation can be preserved.

---

## 10. Self-directed v2.13 prompt

The full technical prompt that was authored and executed for this release is preserved separately as `SELF_PROMPT_v2_13_PERFORMANCE_RELIABILITY.md`. Future agents should read that file before changing any v2.13 performance behavior.

---

## 11. Release status marker

**CURRENT STATUS IN THIS FILE: FINAL / GREEN.** Implementation complete; targeted regressions PASS; seeded v2.12 → v2.13 overlay preservation PASS; clean FULL_SOURCE/PATCH extraction, syntax, and reproducible v2.13 validation PASS. The remaining requirement is real-device field validation on the user's iPhone/Windows installation; do not confuse that field check with a failed release gate.
