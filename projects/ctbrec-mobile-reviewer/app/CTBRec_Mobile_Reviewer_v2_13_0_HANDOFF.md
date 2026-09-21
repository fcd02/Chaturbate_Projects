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
