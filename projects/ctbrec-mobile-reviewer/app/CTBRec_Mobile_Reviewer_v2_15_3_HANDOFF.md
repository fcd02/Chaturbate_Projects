# CTBRec Mobile Reviewer v2.15.3 — Complete Development Handoff

**Release state:** FINAL / GREEN. Final clean-archive validation and v2.15.2→v2.15.3 runtime-preservation overlay gates passed. Baseline v2.15.2 + pending model-open queue race repair. The complete v2.15.2-and-earlier handoff is appended below.

## Standing delivery requirement
Every assistant development response must include downloadable artifacts for: (1) complete current FULL SOURCE ZIP, (2) current safe PATCH/UPDATE ZIP, and (3) complete current HANDOFF Markdown. The user also requests these three files be emailed from/to `christianpwire@gmail.com` with every development response.

## Field symptom that created v2.15.3
The user supplied an iPhone screenshot of an Original model (`halan_jr`) permanently stuck on the v2.15.2 pending screen:
- model header showed `Preparing fi…`;
- mosaic panel showed `Mosaic is being prepared on the PC`;
- detail remained `Opening this model and locating its first chunk…`;
- selection/Submit/Skip/Preview/Rebuild controls were disabled as expected for the `__pending__` placeholder.

The pending surface itself was intentionally added in v2.15.2 to prevent a false Done/model-list bounce while a model's full chunk inventory was still attaching. The bug was that one queue could become permanently detached from the indexing task that was supposed to replace that placeholder.

## Root cause
`prioritize_model_mosaics()` deduplicates model indexing by a shared model key and keeps a set of queue IDs waiting for that model's result. A narrow handoff race existed:
1. an existing model-index task was finishing and had already taken its final snapshot of target queues;
2. a newly opened queue for the same model joined the shared target set and reused that still-running task;
3. the old worker did not see the late queue in its final snapshot;
4. its `finally` block unconditionally removed the entire target set;
5. the late queue therefore remained valid and active but had no chunks and no future worker that could attach them.

Because `/status` saw `preparing_model=True` and `current_mosaic_status.state=running`, the phone correctly kept polling forever. The server also did not mirror task progress into the pending mosaic status, so every kind of delay looked identical to the same vague `Opening this model…` text.

## Implemented v2.15.3 changes

### 1. Late-joining queue targets are no longer discarded
The shared model-index worker now tracks `processed_targets`. Its cleanup removes only queue IDs that the worker actually processed. A target that joins after the final target snapshot remains registered instead of being silently deleted.

### 2. Pending queue self-heal
New `MobileReviewerState._sync_pending_model_task(queue_id, queue_data)` runs from both `/status` and `/current` while a queue has no current chunk and remains in `preparing_model`. It inspects the queue's actual priority task:
- queued/running -> keep waiting; do not duplicate the task;
- error -> surface the real task error;
- done with zero indexed chunks -> surface `No sortable chunks were found`;
- missing/cancelled/done-with-indexed-work but still no attached chunk -> recognize the orphan failure class and re-arm model indexing while this exact queue is still active.

Automatic recovery is bounded to three preparation attempts. If all attempts fail to attach a chunk, the UI receives a real error instead of an infinite spinner.

### 3. Pending screen now shows actual indexing progress
The priority worker mirrors its progress text to every still-pending target queue. The phone can therefore show stages such as existing READY-cache check, sidecar recovery, model indexing, or chunk attachment rather than permanently displaying the initial `locating its first chunk` message.

### 4. Initial placeholder no longer self-restarts before its first task exists
The no-ready open path no longer calls `current_payload()` before `prioritize_model_mosaics()` has created/assigned the initial task ID. This prevents the new self-heal from mistaking the normal few-millisecond bootstrap gap for an orphan.

### 5. v2.15.2 look-ahead semantics unchanged
Do not regress these:
- `upcoming_count=3` means current mosaic + up to 3 future mosaics ready;
- refill occurs after indexing, Submit, Skip and Back;
- current mosaic outranks future look-ahead;
- existing valid mosaics are reused;
- leaving/switching models cancels hidden queue generation;
- no automatic next-model hopping.

### 6. Everything else intentionally preserved
No changes were made to Shift-click selection, Random/average-chunk model ordering, sidecar compatibility rules, video preview fallback, Recu, Keep Last, precise Review cuts, catalog transport, destructive-action safety, or full-library traversal semantics.

### 7. Versioning
- server: `CTBRecMobile/2.15.3`
- PWA cache: `ctbrec-shell-v2153`
- static asset query: `2153`

## Validation
New regression: `validation/validate_v2_15_3_pending_open_recovery.py`. It directly constructs the orphan state and verifies automatic re-arm, verifies running-task progress mirroring without duplication, and verifies zero-chunk terminal handling.

All 24 validation suites under `validation/` passed in development-tree runs and again from the clean extracted final FULL SOURCE in batches. Python compile and JS syntax checks pass. The final FULL SOURCE is sanitized: blank access PIN/secret, empty starter catalog/action queue, empty log, and comment-only recording roots. The final v2.15.2→v2.15.3 PATCH overlay preserved 15 representative protected runtime/private sentinels byte-for-byte, including config, roots, action queue, catalog/READY/library state, NSFW state, model metadata, Recu session/cache/profile, and model-binary sentinel. The PATCH inventory contains none of those protected files.

## Packaging / runtime safety
PATCH must not contain or overwrite:
- `mobile_config.json`;
- `recording_roots.txt`;
- `mobile_action_queue.json`;
- catalog/READY/library/NSFW state;
- model metadata / hidden state;
- Recu session/cache/browser profile;
- generated mosaics or media;
- NudeNet model binaries.

## Exact field test
1. Overlay v2.15.3 patch onto v2.15.2.
2. Restart Reviewer and fully close/reopen the iPhone PWA once.
3. Open a model that has no immediately ready mosaic.
4. Expected: pending panel should show changing real index progress; it must transition to the first mosaic once chunk metadata attaches.
5. Rapidly leave/reopen models, including reopening the same model while preparation is finishing. No queue should remain permanently on `__pending__`.
6. Confirm v2.15.2 look-ahead still keeps current + configured number of future mosaics ready and stops when leaving the model.
7. If a model still fails, collect `mobile_reviewer.log` + diagnostics ZIP; the pending panel should now expose a meaningful stage/error instead of an ambiguous infinite spinner.

## Exact next steps for another AI agent
Use v2.15.3 FULL SOURCE as the baseline. Preserve `_sync_pending_model_task()` self-heal and processed-target cleanup. If a new pending-open field failure appears, inspect the task ID/status/progress and queue `prepare_attempts` before changing chunking or READY semantics. Do not remove the pending placeholder itself; it is what prevents v2.15.2's false-Done race.

---

# Prior complete handoff (v2.15.2 and earlier)
# CTBRec Mobile Reviewer v2.15.2 — Complete Development Handoff

**Release state:** FINAL / GREEN. Baseline v2.15.1 + bounded automatic active-model look-ahead refill and false-Done race fix. The complete prior v2.15.1-and-earlier handoff is appended below.

## Standing delivery requirement
Every assistant development response must include downloadable artifacts for: (1) complete current FULL SOURCE ZIP, (2) current safe PATCH/UPDATE ZIP, and (3) complete current HANDOFF Markdown. The user also requests these three files be emailed from/to `christianpwire@gmail.com` with every development response.

## User request / intended semantics
The user reported that models frequently opened with only one or two mosaics ready; after sorting those, Reviewer returned to the main model list even though additional unsorted chunks existed. Their Open-model look-ahead setting was 3 and they expected the queue to continuously refill.

The required behavior is now explicit:
- `upcoming_count = 3` means **three future mosaics in addition to the current mosaic**;
- if current + only two future mosaics are ready, generate one more future mosaic;
- after sorting current, the window slides and one new tail mosaic is generated;
- continue until the model has no unsorted chunks left;
- never auto-jump to another model;
- leaving/switching models stops that model's look-ahead generation;
- existing ready mosaics must be reused rather than regenerated.

## Why v2.15.1 behaved incorrectly
v2.14.8 intentionally removed automatic future-mosaic prefetch after an earlier complaint about queues building indefinitely and continuing after leaving a model. That fix went too far: `schedule_prefetch()` remained in source but had no automatic call sites. v2.15.1 therefore generated the current mosaic on demand but did not maintain the configured future-ready buffer.

A second race made the symptom worse. `quick_load_queue()` could open only the READY subset while `prioritize_model_mosaics()` asynchronously indexed the full model. Because an already-ready queue was not marked `preparing_model`, a fast user could submit all initially ready chunks before indexing attached the remaining chunk inventory. `current_payload()` then saw an empty queue and returned `done=True`, sending the phone back to the model list even though more chunks were still being discovered.

## Implemented v2.15.2 changes

### 1. Automatic look-ahead restored, but bounded to the active model
`schedule_prefetch(queue_id)` is again called automatically for Original/Review queues after:
- full clicked-model indexing attaches its chunk inventory;
- Submit / Submit-fast;
- Skip / Skip-fast;
- Back / Back-fast.

The maintenance loop also retries the current visible queue every ~8 seconds. This is a self-heal path for a refill that temporarily stopped because the PC was active, current mosaic had priority, or model metadata was still indexing. It does not schedule hidden queues.

### 2. Upcoming count is future-only
The worker uses `chunks[1:target+1]`, intentionally excluding the current chunk. Thus Upcoming=3 means current + up to three future ready mosaics.

### 3. Refill worker dynamically re-snapshots the queue
The automatic worker no longer takes one fixed list and exits. After every generated mosaic it re-reads the active queue. If the user submits the current chunk while refill is in flight, the window slides and the newly exposed tail chunk is pulled into the same bounded refill pass. This avoids waiting for another manual action or maintenance tick.

### 4. Existing ready work is reused
Before generating anything, every future chunk in the bounded window is checked with the existing exact `_chunk_mosaic_ready()` validation. Ready mosaics are skipped. Only missing/invalid mosaics in the active window are generated. Chunks beyond the configured window are untouched.

### 5. Current mosaic retains priority
If the current chunk has an interactive mosaic future running, look-ahead waits in `paused_priority` and resumes automatically as soon as that current mosaic finishes. It does not compete with the mosaic the user is waiting to see.

### 6. Idle-only setting remains authoritative
If `idle_only` is enabled for the mode and Windows reports insufficient PC idle time, automatic refill returns to `waiting_idle`. The active-queue maintenance retry restarts it later. Manual Fill/Generate Ahead remains the explicit user override path.

### 7. Leaving/switching models still cancels hidden generation
The v2.14.8 `_deactivate_queue_work_locked()` behavior is preserved. It sets `prefetch_cancel_requested`, cancels model-priority demand, and the look-ahead worker verifies that its queue is still the active phone queue. No return to the old runaway "keep building after I leave" behavior.

### 8. False-Done race fixed
When `open_model_sort_first()` opens an existing READY subset, it now sets that queue's `preparing_model=True` until the clicked-model indexing worker attaches the full current chunk inventory. If the user consumes the ready subset before indexing finishes, `/current` returns the existing `__pending__` preparing surface instead of `done=True`. Once inventory attaches, `_merge_model_chunks_into_queue()` clears `preparing_model` and the remaining chunks appear in the same queue.

### 9. UI/status language updated
The review panel now describes **Automatic Look-ahead** and the Settings help explicitly states that Upcoming mosaics counts future mosaics in addition to the current one. The manual button remains available as `Fill now`. Open-model toast language now reflects automatic refill rather than v2.14.8's on-demand-only wording.

### 10. Versioning
- server: `CTBRecMobile/2.15.2`
- PWA shell: `ctbrec-shell-v2152`
- static query version: `2152`

## Validation
New suite: `validation/validate_v2_15_2_active_lookahead.py`.

It functionally constructs a six-chunk active queue with Upcoming=3 and only current + two future chunks initially ready. It proves:
1. first refill generates only the missing third future mosaic;
2. the fourth future mosaic is untouched because it lies outside the window;
3. after current is removed, refill generates exactly one newly exposed tail mosaic;
4. the next chunk beyond the window stays untouched;
5. switching active queue prevents additional hidden generation.

Source/static invariants also verify the full-inventory `preparing_model` guard, refill call sites, maintenance retry, and release identity.

All 23 validation suites pass in the source tree. Final clean-archive extraction and v2.15.1→v2.15.2 runtime-preservation patch-overlay gates also passed before delivery.


## Final packaging gates — PASS
- Final FULL SOURCE archive extracted to a clean directory: Python/JS syntax PASS and all 23 validation suites PASS.
- Final v2.15.1→v2.15.2 PATCH overlaid onto a seeded baseline: protected runtime/private sentinels remained byte-for-byte unchanged.
- PATCH inventory contains no active config, roots, action queue, catalog/READY/library/NSFW state, model metadata, Recu session/profile, logs, media, or ONNX model payload.

## Behavior intentionally preserved
- No automatic move to next ready model.
- Leaving/switching models cancels hidden active-model generation.
- v2.15.1 Shift-click range selection.
- v2.15.0 Random + size filters + Avg chunk model ordering.
- v2.14.9 method-compatible mosaic reuse / scoped sidecar recovery.
- v2.14.8 exact-model opening, no 12-second model-open timeout, playback failover.
- Current-mosaic generation outranks background/look-ahead work.
- READY/index, Recu, Keep Last, precise Review cuts, catalog transport, and destructive-safety invariants.

## Packaging safety
PATCH must not include active/private runtime state, including at minimum `mobile_config.json`, `recording_roots.txt`, `mobile_action_queue.json`, catalog/READY/library/NSFW state, Recu session/profile/cache, model metadata, logs, generated mosaics/media, or NudeNet model binaries.

## Exact next steps for another AI agent
1. Use v2.15.2 FULL SOURCE as baseline after final package validation passes.
2. If look-ahead appears not to refill, first inspect the current queue's `prefetch_status`, active queue lease, `background_mosaics.<mode>.enabled`, `upcoming_count`, and `idle_only` settings.
3. Do not "fix" a paused idle-only queue by ignoring the user's idle setting.
4. If a model exits early again, inspect whether `preparing_model` was cleared before full chunk inventory attached and compare `known_total_chunks`, current queue length, and history count.
5. Preserve the active-queue-only cancellation boundary; do not restore generation for abandoned queues.
6. Keep every development response packaged as FULL SOURCE + PATCH + current HANDOFF and email those three artifacts as requested.

---

# Prior complete handoff (v2.15.1 and earlier)

# CTBRec Mobile Reviewer v2.15.1 — Complete Development Handoff

**Release state:** FINAL / GREEN. Clean FULL SOURCE + v2.15.0 patch-overlay preservation gates passed.
**Baseline:** v2.15.0. This release is intentionally limited to desktop Shift-click range selection and version/docs/tests.

## Standing delivery requirement
Every development response must include the complete current FULL SOURCE ZIP, safe PATCH ZIP, and complete current HANDOFF Markdown. The user also requests those three artifacts emailed from/to christianpwire@gmail.com.

## User request
Add Windows-like Shift-click range selection without disturbing the currently stable behavior. The user clarified that no Ctrl/Ctrl+Shift behavior is needed because normal clicks already preserve other selections. Required semantics:
- ordinary click remains exactly as it already works;
- Shift-click selects/applies the current action to everything between the immediately previous click and the new click, inclusive;
- prior selections outside that range remain untouched.

## Implemented behavior

### 1. File/segment Shift-click range selection
`static/app.js` now tracks a UI-only `lastFileSelectionIndex` anchor.

For sortable file/segment tiles and their file-list rows:
- a normal click still executes the pre-v2.15.1 toggle behavior;
- a Shift-click builds the inclusive ordered range between the previous clicked file and the newly clicked file;
- every item in that range is placed into the currently selected action/state rather than toggled one-by-one;
- selections outside the range are preserved.

Mode-specific range behavior:
- **Original:** every file in the range is added to KEEP.
- **Review:** every file in the range is assigned the currently active destination/action (`Leave for review`, `DELETE`, `Cumshots`, or `Misc Hot Scenes`).
- **Deletion:** restorable files in the range are added to SEND BACK; non-restorable files are skipped safely.
- **Non-NSFW Cleanup:** every file in the range is added to the delete selection.

The newly clicked endpoint becomes the anchor for the next Shift-click, matching the user's requested “most recent click to next click” behavior.

### 2. Review Frame Cut Shift-click range selection
Frame Cut gets the same behavior with an independent `lastFrameSelectionIndex` anchor:
- normal frame clicks preserve the existing toggle/clear behavior;
- Shift-click applies the current frame destination across every sampled frame between the prior frame click and the new frame click, inclusive;
- selecting Delete/Clear across a frame range clears those frame decisions back to DELETE;
- prior frame selections outside the range remain unchanged.

### 3. Anchors never cross chunks/models
Both range anchors reset whenever `loadCurrent(true)` loads/resets selections for a new chunk/model. Same-chunk metadata/status refreshes do not reset them.

### 4. Performance / rendering behavior
Range selection updates only the affected tile classes/file-row states and renders the summary once. It does not rebuild queues, regenerate mosaics, touch disk metadata, invoke FFmpeg, or alter READY state.

### 5. Explicit non-changes
v2.15.1 does **not** change:
- v2.15.0 Random / Avg chunk model sorting;
- v2.14.9 scoped sidecar recovery / compatible older mosaic reuse;
- v2.14.8 exact-model open, demand-only generation, queue cancellation, or video failover;
- mosaic/chunk generation algorithms;
- whole-library traversal/background scheduling;
- Recu;
- Keep Last;
- destructive action-queue semantics;
- existing ordinary click/tap selection behavior.

Mobile taps naturally continue through the ordinary-click path because mobile tap events do not set `shiftKey`.

## Versioning
- server: `CTBRecMobile/2.15.1`
- PWA cache: `ctbrec-shell-v2151`
- static asset query version: `2151`
- model-sort preference key intentionally remains `ctbrec_model_sort_v2150` so upgrading does not wipe the user's v2.15.0 sort/bounds/seed preferences.

## Validation added
New suite: `validation/validate_v2_15_1_shift_range.py`.

It protects:
- event propagation of `shiftKey` from both mosaic tiles and file rows;
- separate file/frame anchors;
- inclusive range calculation;
- additive range application rather than range toggling;
- unchanged ordinary-click toggle code;
- anchor reset on new chunk/model selection state;
- v2.15.1 server/cache/static markers.

All historical suites remain required.

## Final validation / packaging gate
- Python compilation: PASS.
- `static/app.js`, `static/rapid.js`, `static/service-worker.js` syntax: PASS.
- Source tree: all **22/22** `validation/*.py` suites PASS.
- Clean-extracted FINAL FULL SOURCE: all **22/22** suites PASS.
- v2.15.0→v2.15.1 PATCH overlay: protected active runtime/private state remained byte-for-byte unchanged.
- PATCH blocklist inspection: PASS; no runtime/private state, media, generated mosaics, Recu credentials/profile, or ONNX model payload is included.

## Exact next steps for another agent
1. Treat v2.15.1 as v2.15.0 plus client-side Shift-click range selection only.
2. Preserve ordinary click/tap behavior exactly; do not reinterpret this feature as Windows single-click replacement semantics.
3. Keep file and frame anchors independent and reset them on chunk/model changes.
4. Do not add Ctrl/Ctrl+Shift behavior unless the user later explicitly asks for it.
5. Keep range selection local to UI state; it must never trigger indexing/generation/disk work.
6. Every development response must ship FULL SOURCE ZIP + PATCH ZIP + current HANDOFF Markdown and email those three artifacts as requested.

---

# Prior complete handoff (v2.15.0 and earlier)
# CTBRec Mobile Reviewer v2.15.0 — Complete Development Handoff

**Release state:** FINAL / GREEN. Clean FULL SOURCE + patch-overlay preservation gates passed.
**Baseline:** v2.14.9. This release intentionally leaves the working v2.14.9 queue/mosaic/playback/background behavior unchanged.

## Standing delivery requirement
Every development response must include the complete current FULL SOURCE ZIP, safe PATCH ZIP, and complete current HANDOFF Markdown. The user also requests those three artifacts emailed from/to christianpwire@gmail.com.

## User request
Add two model-list ordering workflows while preserving the currently stable sorter:
1. Random ordering with configurable minimum/maximum model sizes.
2. Largest average chunk size per model to smallest.

The user explicitly emphasized that v2.14.9 is working wonderfully and must not be destabilized.

## Implemented behavior

### 1. Model-order controls
The Models view now exposes:
- **Largest total** — existing/default total-byte descending order.
- **Random** — deterministic pseudo-random order.
- **Avg chunk size ↓** — known average chunk bytes descending.

The controls apply to Originals, Review, Easy Sort, and Hidden. Deletion and Non-NSFW Cleanup intentionally retain their established safety-specific ordering.

### 2. Random mode with size bounds
Random mode has optional **Min model GB** and **Max model GB** inputs. These filter on the catalog's existing total `bytes` value and therefore require no server work. Blank means unbounded.

Random order is intentionally stable. A persisted seed plus model name is hashed client-side; ordinary rerenders, searches, catalog refreshes, and returning from a model do not reshuffle the list. **Reshuffle** advances the seed and creates a new order. Sort mode, bounds, and seed persist in iPhone localStorage under `ctbrec_model_sort_v2150`.

### 3. Average chunk size mode without new disk work
The server now adds read-only fields to normal Original/Review catalog rows when a trustworthy READY snapshot is already available:
- `avg_chunk_bytes`
- `avg_chunk_size`
- `chunk_count`
- `chunk_stats_known`
- `chunk_stats_updated_at`

A READY snapshot is used only when BOTH of these match the current catalog aggregation:
- snapshot drive set == the model's current selected-drive set;
- snapshot stored model-byte total == current catalog model-byte total.

Average chunk bytes are calculated from the persisted chunk definitions' `source_bytes`. This does not validate/open JPEGs and does not mutate the READY snapshot.

If a model has not yet been indexed, or its snapshot does not match the current drive/byte scope, no estimate is invented. In Avg chunk mode:
- known models sort by average chunk bytes descending;
- unknown models follow, ordered by total model bytes descending;
- the UI reports known/total coverage and labels unknown cards `avg chunk pending model indexing`.

This design is intentional: selecting Avg chunk must never launch a 4,000-model metadata crawl, FFmpeg, mosaic generation, or compete with active sorting merely to populate an ordering metric. Normal existing indexing/background work gradually fills the statistic.

### 4. Derived catalog cache correctness
The existing short-lived catalog view cache already stored the READY-index timestamp but did not compare it on cache hits. Since v2.15.0 exposes READY-derived average-chunk metadata, cache reuse now also requires `cached_ready_stamp == ready_stamp`. This only rebuilds cheap derived in-memory catalog rows when the READY index changes; it does not touch disk or alter queue behavior.

### 5. Explicit non-changes
v2.15.0 does NOT modify:
- `open_model_sort_first`, pending exact-model open, or 12-second timeout fixes;
- demand-only current mosaic generation;
- Generate Ahead semantics;
- queue cancellation when leaving a model;
- v2.14.9 scoped sidecar recovery / compatible old mosaic reuse;
- chunk generation algorithms;
- FFmpeg mosaic creation;
- video preview fallback ladder;
- Recu;
- Keep Last;
- destructive action queue;
- whole-library scheduling/traversal.

## UI details
- Sort controls live below model search.
- Random size inputs use GB with decimal values accepted.
- Random filters apply only in Random mode.
- Search composes with the active ordering/filter.
- Progressive 120-card rendering is preserved.
- Average chunk details are shown on cards only while Avg chunk mode is active.

## Validation added
`validation/validate_v2_15_0_model_sorting.py` verifies:
- v2.15.0 version/cache/static asset markers;
- Random / Avg chunk controls and min/max/reshuffle UI exist;
- deterministic random code and persistent preferences exist;
- server exposes READY-backed chunk statistics;
- matching drive+byte snapshots yield correct average chunk bytes;
- stale byte totals are rejected;
- mismatched drive scopes are rejected;
- READY-index timestamp changes invalidate the derived catalog cache;
- Avg mode explicitly remains non-scanning/non-generating.

Historical validation suites are also required unchanged except version-marker assertions advanced to v2.15.0.

## Final packaging gate
- Source tree: all 21 `validation/*.py` suites PASS.
- Python compilation and `node --check` for app/rapid/service-worker: PASS.
- Clean-extracted candidate FULL SOURCE: all 21 suites PASS.
- v2.14.9→v2.15.0 PATCH overlay: protected runtime/private sentinels remained byte-for-byte identical, including config, roots, action queue, catalog/READY/library/Non-NSFW state, model metadata, Recu session/profile, and model-binary sentinel.
- PATCH inventory contains no runtime/private state, mosaics/media, or ONNX model payload.

## Packaging rules
PATCH must exclude runtime/private state, including config, roots, action queue, catalog/READY/library/NSFW state, logs, Recu session/profile/cache, user model metadata, mosaics/media, and unchanged NudeNet model binaries.

## Exact next steps for another agent
1. Treat v2.15.0 as v2.14.9 + isolated model-list sorting UI/read-only metadata only.
2. Do not replace READY-backed averages with automatic model crawling unless the user explicitly chooses that tradeoff.
3. Preserve stable random ordering; do not use fresh `Math.random()` on every render.
4. If the user later wants Avg chunk coverage for every unindexed model immediately, design a separate opt-in low-priority metadata indexing job rather than piggybacking on catalog GET or active sorting.
5. Any future change must keep v2.14.9 queue/mosaic/playback invariants intact.
6. Every development response must ship FULL SOURCE + PATCH + current HANDOFF.

---

# Prior complete handoff (v2.14.9 and earlier)
# CTBRec Mobile Reviewer v2.14.9 — Complete Development Handoff

**Release state:** FINAL / GREEN for automated validation. v2.14.8 baseline + scoped sidecar-recovery / truthful reindex-status correction below. The complete v2.14.8-and-earlier handoff is appended after this authoritative section.

## Standing delivery requirement
Every assistant development response for this project, complete or incomplete, must provide:
1. the complete current FULL SOURCE ZIP;
2. the current safe PATCH/UPDATE ZIP;
3. a complete current Markdown handoff containing implemented + planned work, validation, known issues, architecture/context, and exact resume steps.

The user additionally requires those three artifacts to be emailed from/to `christianpwire@gmail.com` whenever Gmail tooling is available.

## User field report that created v2.14.9
The user observed that after using **Rebuild chunk + mosaic** on one chunk/model, many subsequently opened models appeared to report that their mosaics had been "re-indexed." They were concerned that:
- a single chunk rebuild might have triggered a library-wide re-index/regeneration;
- the first ~1,000+ models already processed by the whole-library worker might lose or stop using their existing mosaics;
- older mosaics created by recent releases should remain reusable when they were created with the same trustworthy methodology, even if not created by the newest software version.

## Root-cause findings

### 1. Rebuild chunk + mosaic is local, not global
`rebuild_current_chunk_task(queue_id)` only operates on the current queue's model and selects the fresh disk chunk that overlaps the currently viewed chunk. It purges/recreates the mosaic artifacts tied to that old/fresh source set and then persists the rebuilt snapshot for **that model only**.

It does **not** iterate the whole catalog and does not reset/rebuild every model.

### 2. v2.14.8 did run sidecar recovery on every clicked model
The misleading behavior came from the separate clicked-model priority path. `prioritize_model_mosaics()` unconditionally called `_recover_partial_snapshot_from_existing_mosaics()` every time a model was opened, even when the READY cache already covered every valid sidecar-backed mosaic for that model.

This was unnecessary disk/JSON/stat work. More importantly, `_merge_ready_chunks_into_queue()` then overwrote the queue status with:

`Existing mosaics were reindexed...`

even when the recovery found **zero additional mosaics**. Therefore the phone could truthfully be showing old mosaics while still falsely reporting that the model had just been reindexed.

### 3. The whole-library cursor does not prove all earlier models used today's validation methodology
The durable whole-library traversal intentionally survives pause/restart and software upgrades. Therefore a pass currently at e.g. model 1,236 may include earlier models that were processed under an older release before later mosaic-integrity validation was introduced.

A model being "behind the cursor" means that pass already advanced past it; it does **not** guarantee its stored mosaic/hit-map was produced by the newest compatible methodology.

This does not mean all those mosaics are bad. The current code evaluates compatibility structurally, not by release number.

### 4. Existing mosaic reuse is methodology/capability based, not version based
`_embedded_layout_entry()` accepts embedded mobile layout metadata version 2+ and validates:
- current source identity/order alignment;
- matching chunk signature;
- matching recorded outputs;
- actual JPEG dimensions;
- valid in-bounds tap rectangles;
- generation-plan/tile ordering when enough generation metadata exists;
- stricter frame/timestamp checks for v3 layouts;
- no duplicate hit boxes.

There is no `v2.14.x` release-number gate in this validation path. Therefore a mosaic created several releases ago remains reusable when its metadata and geometry satisfy the same trustworthy method. A structurally unsafe/legacy mosaic is rejected and regenerated only when it becomes current or when the user explicitly rebuilds/generates it.

## Implemented v2.14.9 changes

### 1. Sidecar recovery is now conditional
The clicked-model priority worker now compares:
- the number of already loaded READY mosaics for the exact queue/model; versus
- the cheap count of valid sidecar metadata files on disk.

It calls `_recover_partial_snapshot_from_existing_mosaics()` **only when `sidecar_count > loaded_ready`**.

If the READY cache already covers the compatible sidecars, the worker reports internally that no sidecar reindex is needed and proceeds directly to lightweight chunk indexing.

This preserves the v2.14.7 repair mechanism for genuinely stale READY indexes while stopping needless recovery scans on every click.

### 2. Reindex/recovery status is now truthful
`_merge_ready_chunks_into_queue()` updates the phone's status only when recovery actually contributes one or more new queue chunks.

The status now says:

`Recovered N additional valid existing mosaic(s) from disk metadata...`

If zero additions were recovered, no "reindexed" status is emitted.

### 3. No blanket invalidation of older compatible mosaics
No release-version cutoff was added. Existing mosaics continue to be reused based on current source identity, embedded layout/hit-map integrity, dimensions, and generation-plan compatibility.

This is deliberately aligned with the user's requirement: preserve expensive prior mosaic work whenever the **method** is compatible, not merely when the file was generated by the latest release.

### 4. Rebuild remains current-chunk/current-model scoped
No change broadens `Rebuild chunk + mosaic`. It remains a targeted repair command for the current chunk/source set. It does not schedule whole-library reindexing or regeneration.

### 5. Versioning
- server: `CTBRecMobile/2.14.9`
- PWA shell: `ctbrec-shell-v2149`
- static asset query: `2149`

## Validation
New suite:

`validation/validate_v2_14_9_sidecar_reindex_scope.py`

It protects:
- current v2.14.9 release/cache markers;
- sidecar recovery only after `sidecar_count > loaded_ready`;
- explicit no-reindex path when READY already covers compatible mosaics;
- queue status changes only when real additions exist;
- no false legacy `Existing mosaics were reindexed` message in the merge path;
- mosaic reuse remains based on embedded layout/source/signature capability rather than `v2.14.x` release number.

All 20 `validation/validate_*.py` suites pass in the v2.14.9 source tree, including the complete v2.14.8 queue/video suite and v2.14.7 stale-index recovery suite.

Final packaging gates also passed:
- clean-extracted FULL SOURCE had blank PIN/secret and empty starter action state; Python/JS syntax passed; all 20 validation suites passed from extracted bytes;
- PATCH inventory contains no config, roots, READY/catalog/library state, action queue, Recu session/profile, model metadata, log, media, or model binary;
- PATCH overlaid onto a seeded v2.14.8 install while preserving all protected runtime-state sentinels byte-for-byte;
- v2.14.9 scoped-recovery and v2.14.8 on-demand/video regressions passed from the overlay tree.

## Interpretation of the user's current field observation
- Seeing "reindexed" on many models in v2.14.8 does **not** prove those JPEG mosaics were regenerated, deleted, or replaced. The message could appear with zero newly recovered mosaics.
- Seeing "Generating mosaic on PC" after one or two chunks means the newly-current chunk did not have a mosaic/hit-map that passed current validation or was not represented by a compatible ready sidecar. It is not evidence by itself of a library-wide rebuild.
- A whole-library cursor such as `1,236 / 4,167` does not guarantee every earlier model was processed under the same code/method if the persistent pass crossed upgrades.
- v2.14.9 does not destroy or roll back existing mosaic files. It reduces unnecessary reindex work and continues to reuse all structurally compatible mosaics already on disk.

## If a specific previously-processed model still starts regenerating unexpectedly
Do not delete or overwrite its old mosaics. Collect:
1. that model's complete `._chunk_mosaics` directory (JPEGs + `.sources.json` sidecars);
2. that model's `._cb_chunk_reviewer_state.json`;
3. the install's `mobile_ready_work_index.json`;
4. preferably a fresh `collect_mobile_diagnostics.bat` ZIP and `mobile_reviewer.log`.

Compare the exact sidecar signatures/source identities and validation failure before changing compatibility rules. Orphan JPEGs without trustworthy source/hit-map metadata must remain ignored rather than guessed.

## Exact next steps for another AI agent
1. Use v2.14.9 FULL SOURCE as the baseline.
2. Preserve v2.14.8 exact-model/demand-only generation and video failover behavior.
3. Preserve v2.14.7 stale READY repair, but keep v2.14.9's condition: only invoke per-click sidecar recovery when disk sidecars can add valid work beyond what the queue already loaded.
4. Never tie mosaic reuse to a software release number; validate capability/metadata/source identity instead.
5. Do not broaden Rebuild chunk + mosaic beyond the current model/chunk unless explicitly requested.
6. If a behind-cursor model regenerates, inspect its sidecars/READY snapshot before assuming the whole-library worker failed.
7. Run all validation suites + clean package validation + patch runtime-preservation checks for future releases.
8. Every development response must provide and email FULL SOURCE ZIP + PATCH ZIP + current HANDOFF Markdown.

---

# Prior complete handoff (v2.14.8 and earlier)
# CTBRec Mobile Reviewer v2.14.8 — Complete Development Handoff

**Release state:** FINAL / GREEN for automated source + packaging validation. v2.14.7 baseline + exact-model on-demand open, queue cancellation, and video playback recovery changes below. The complete v2.14.7-and-earlier handoff is appended after this authoritative section.

## Standing delivery requirement
Every assistant development response for this project, complete or incomplete, must provide:
1. the complete current FULL SOURCE ZIP;
2. the current safe PATCH/UPDATE ZIP;
3. a complete current Markdown handoff containing implemented + planned work, validation, known issues, architecture/context, and exact resume steps.

The user additionally requested that these three artifacts be emailed to `christianpwire@gmail.com` on every development response when email tooling is available.

## User field request that created v2.14.8
The user explicitly rejected the behavior that could move to another ready model when the model they tapped had no immediately ready mosaic. Required behavior:
- if the user taps a model, Reviewer must open that exact model and generate its mosaic if necessary;
- no automatic next-ready-model substitution;
- stop the queue from continuously building future mosaic work;
- when the user leaves a model, stop generating more mosaics for that model;
- fix frequent video playback failures from disk and phone streaming;
- fix the near-universal ~12-second timeout when tapping a model.

## Root-cause findings

### 1. The ~12-second model-open failure was real and explicit in the client
`static/app.js::openModel()` called `/api/queues/open-fast` with `timeoutMs: 12000`.

The server's open path could still need to inspect READY metadata, reconcile existing sidecar-backed mosaics, or index model chunks. Therefore a healthy PC operation lasting longer than 12 seconds could be aborted by the phone and surfaced as the reported timeout even though the server/model was not fundamentally inaccessible.

### 2. Clicked-model priority work generated the entire model
The pre-v2.14.8 `prioritize_model_mosaics()` path walked the model chunk list and generated missing mosaics across the model. This directly contradicted the desired interactive model: the user only needs the mosaic they are currently sorting.

### 3. Future mosaic work was also scheduled from navigation/maintenance paths
Automatic `schedule_prefetch()` calls existed around normal queue progression/maintenance. This could make a queue continue accumulating generation work beyond what the user was actively viewing.

### 4. Leaving a model did not revoke all model-specific generation demand
The sorting heartbeat tracked whether the phone was active, but the old queue/priority targets did not consistently translate leaving/switching models into cancellation of model-specific future work.

### 5. Video preview had incomplete recovery for common field failure modes
The prior preview architecture had useful direct-disk/HLS/remux paths, but two practical gaps remained:
- a stream can stall without firing an immediate browser `error` event;
- a failure did not consistently cascade through every available transport/codec fallback.

FFmpeg preview paths also benefited from more tolerant timestamp/corrupt-packet handling for truncated or imperfect CTBRec recordings.

## Implemented v2.14.8 changes

### 1. Exact clicked model always opens
`open_model_sort_first()` now always represents the exact requested model.

If READY work exists, it opens normally. If nothing is immediately ready, server creates a **pending queue immediately** with:
- the requested model name;
- `preparing_model=True`;
- a synthetic pending current state (`signature="__pending__"`);
- status text telling the phone that the model is being opened/indexed.

The UI enters the Review view for that queue rather than staying on Models or substituting another model.

### 2. Removed the 12-second `open-fast` client abort
Both the normal PWA `openModel()` and Rapid Sort override now use:

`timeoutMs: 0`

for `/api/queues/open-fast`.

More importantly, the server now returns a pending queue promptly instead of requiring all expensive reconciliation to finish before the phone can enter the model.

### 3. v2.14.7 sidecar repair remains, but it is moved off the tap-critical path
`quick_load_queue()` gained `recover_sidecars: bool = True`.

- Normal/direct calls retain the v2.14.7 behavior by default.
- `open_model_sort_first()` calls it with `recover_sidecars=False` so a large sidecar directory cannot block the phone tap.
- The clicked-model priority worker then performs `_recover_partial_snapshot_from_existing_mosaics()` asynchronously and merges trustworthy ready chunks into the live queue.

All v2.14.7 safeguards remain:
- `mobile_ready_work_index.json` is still only a derived cache;
- valid sidecars/current file identity remain authoritative for already-built mosaics;
- Original chunks marked `done`/`skipped` are not resurrected;
- orphan JPEGs without trustworthy sidecars/hit maps remain ignored;
- manual Generate Ahead still reindexes sidecars before deciding future work.

### 4. Current-only automatic mosaic generation
`prioritize_model_mosaics()` was changed from a full-model generation loop into:
1. sidecar recovery;
2. chunk metadata/index build;
3. persist READY snapshot;
4. attach one stable chunk inventory to the queue;
5. if the queue is still the active phone queue, request **only its current mosaic**.

It returns policy metadata `current-only` and reports `generated: 0` because actual current generation is delegated to the interactive mosaic request.

When the user submits/skips/backs into another chunk, the existing queue progression code requests that newly-current chunk's mosaic. This is the desired on-demand behavior.

### 5. Automatic future prefetch removed
There are no remaining call sites to `schedule_prefetch()` in the server; only the helper definition remains for compatibility.

Automatic calls were removed from normal load/navigation/maintenance flow. Future mosaics are generated only when:
- they become the current chunk; or
- the user explicitly invokes **Generate Ahead**.

The UI now labels that panel/action as optional rather than implying automatic prefetch.

### 6. Leaving/switching models cancels hidden model-specific work
New `_deactivate_queue_work_locked(queue_id)`:
- sets `prefetch_cancel_requested=True`;
- marks active prefetch state cancelled;
- removes the queue from `priority_generation_targets`;
- if no target remains for that priority model task, requests TaskRecord cancellation.

`mark_sort_session_active(queue_id=...)` deactivates the previous queue when switching.

`mark_sort_session_inactive()` deactivates the previous queue immediately when the Review view is left (the existing client sorting heartbeat posts `active:false` when leaving the view).

`request_interactive_mosaic()` now wraps its progress callback with an active-queue guard. At each generation progress checkpoint it verifies:
- the sorting lease is active; and
- this exact queue is still `active_sort_queue_id`.

If not, it raises `TaskCancelled` and reports cancellation rather than progressing into more model work.

Manual `prefetch_task()` / Generate Ahead also checks `prefetch_cancel_requested` before each upcoming mosaic and stops if the user left the model.

**Practical cancellation boundary:** an FFmpeg operation already inside one low-level extraction call may finish before the next progress checkpoint. The important invariant is that leaving the model cannot keep starting additional mosaic work indefinitely.

### 7. No automatic next-ready-model action remains
The previous open behavior that could select a ready suggestion has been removed.

`nextReadyModel()` remains only as an explicit manual UI action through the Next Ready Model buttons. The Ignore-model handler no longer calls it automatically; after Ignore it returns to Models.

### 8. Video playback fallback ladder
`playPreviewItem()` now tracks a playback stage:
- direct/local disk or native HLS;
- fast remux;
- compatibility transcode.

Failure behavior:
- direct/HLS error or startup stall -> automatic `remux_url`;
- remux error or startup stall -> automatic `compatibility_url`;
- compatibility failure -> terminal message that the source may be truncated/damaged.

### 9. Video stall detection
A source can hang without firing `video.onerror`. v2.14.8 arms startup fallback timers:
- native HLS: 10s;
- direct desktop/original file: 8s;
- normal remux stage: 12s;
- compatibility stage: 20s.

Timers clear on `loadedmetadata`/`canplay` and are cleaned up when the preview closes or ends.

### 10. More tolerant FFmpeg preview/HLS generation
HLS segment generation adds:
- `-fflags +genpts+discardcorrupt`
- `-err_detect ignore_err`
- bounded subprocess timeout based on requested segment length.

Preview remux/stream generation adds:
- `-fflags +genpts+discardcorrupt`
- `-err_detect ignore_err`
- `-avoid_negative_ts make_zero`
- fragmented MP4 flags `+frag_keyframe+empty_moov+default_base_moof`.

These changes improve playback resilience for CTBRec files with imperfect timestamps/truncated packets without modifying source media.

### 11. Versioning
- server: `CTBRecMobile/2.14.8`
- PWA shell: `ctbrec-shell-v2148`
- static asset query: `2148`

## Validation
New suite:

`validation/validate_v2_14_8_on_demand_queue_video.py`

It protects:
- v2148 release markers;
- no 12-second timeout in either normal or Rapid Sort model open;
- no suggestion/auto-hop in exact-model open;
- pending queue construction for unready clicked model;
- sidecar recovery explicitly skipped only on tap-critical quick-open;
- no automatic `schedule_prefetch()` call sites;
- current-only priority generation policy;
- queue deactivation/cancellation hooks;
- no automatic call to `nextReadyModel()`;
- direct/HLS -> remux -> compatibility fallback and stall timers;
- tolerant FFmpeg flags and bounded HLS segment timeout.

Historical preview regression was updated to recognize the stronger fallback ladder rather than the older one-step remux expression.

All 19 `validation/validate_*.py` suites passed in the v2.14.8 source tree, including:
- mobile recovery/transport and 4,100-model browser DOM/race coverage;
- READY-count preservation / exact signature hydration;
- settings corruption/write guard;
- Original/Review/NSFW FFmpeg batching;
- direct-disk HTTP Range preview;
- Recu incremental/auth/capture/fallback suites;
- scroll reset;
- v2.14.4 mosaic integrity/rebuild;
- v2.14.5 tagged parser;
- v2.14.6 per-session/cross-drive Keep Last;
- v2.14.7 stale ready-index sidecar reindex;
- v2.14.8 on-demand queue/video regression.

Final packaging gates also passed:
- candidate FULL SOURCE ZIP extracted cleanly; fresh-install PIN/secret were blank and shipped log/action state were sanitized; Python/JS syntax passed; all 19 validation suites passed from extracted bytes;
- PATCH inventory contained no protected runtime/private/model files;
- PATCH overlaid onto a seeded v2.14.7 install without changing hashed runtime sentinels;
- v2.14.7 sidecar-recovery and v2.14.8 on-demand/video regressions passed from the overlay tree.

## Safety invariants preserved
- PATCH must never overwrite active runtime/private files.
- No native CTBRec `models.json` mutation fallback.
- Destructive queue/file operations remain durable/idempotent.
- Precise Review cut outputs verify before original deletion/move.
- Recu remains enrichment-only and may not block sorting.
- Whole-library and NSFW background workers remain separate from the new per-opened-model demand policy.
- v2.14.6 Keep Last semantics remain unchanged.
- v2.14.7 source-identity/sidecar recovery rules remain unchanged outside the tap-latency relocation described above.

## Known limitations / field validation
1. **Real iPhone/Windows playback still needs field confirmation.** Automated/static regressions can verify the fallback architecture and server flags but cannot reproduce every damaged/odd source file or Safari decoder behavior.
2. **Current FFmpeg subprocess cancellation is checkpoint-bounded.** Leaving a model prevents further mosaic operations from being started, but an already-running low-level FFmpeg extraction may finish before cancellation is observed.
3. **Truly corrupt media can still be unplayable.** v2.14.8 now exhausts direct/HLS, remux, and compatibility paths before declaring failure; it does not fabricate playable media from irrecoverably damaged sources.
4. **Chunk metadata is still attached to the live queue.** What was removed is continuous future *generation*. The queue holds one stable model inventory so Submit/Skip can advance without rebuilding the entire model every time.
5. The global whole-library mosaic worker still exists as its own user-configured background feature. v2.14.8 specifically stops hidden per-click/model-prefetch buildup; it does not delete the optional whole-library feature.

## Field-test checklist
1. Apply v2.14.8 PATCH over v2.14.7; restart Reviewer; fully kill/reopen iPhone PWA.
2. Tap a model that currently has no ready mosaic. Confirm the exact model opens immediately into a Preparing state and eventually shows its own first mosaic; no other model opens automatically.
3. Tap a large model and wait for first mosaic. Return to Models before it finishes. Confirm server/Task UI does not keep generating additional mosaics for that model.
4. Reopen that model. Confirm it can resume/index and produce the current mosaic.
5. Sort/Submit the current mosaic. Confirm only the newly-current mosaic begins generation; later mosaics do not build continuously.
6. Tap Generate Ahead manually and verify only that explicit request pre-generates future mosaics; leave the model and confirm it cancels/stops at the next safe checkpoint.
7. Preview several known-problem files on the PC and phone. Confirm a stalled/failed direct/HLS attempt automatically retries remux and then compatibility if needed.
8. Scrub/seek large MP4/MOV sources to retain v2.14.1 Range behavior.
9. Recheck a model that relied on v2.14.7 sidecar recovery to confirm existing sidecar-backed mosaics still appear and done/skipped chunks stay excluded.

## Exact next steps for another AI agent
1. Use the v2.14.8 FULL SOURCE ZIP as the baseline, not v2.14.7.
2. Preserve the exact-model contract: an unready clicked model must still open its own pending queue; never restore ready-model substitution.
3. Preserve demand-only generation: current chunk automatic, Generate Ahead explicit; do not reintroduce automatic `schedule_prefetch()` call sites.
4. Preserve queue deactivation on model switch/Back and the active-queue guard inside interactive mosaic progress.
5. If field reports say leaving still consumes FFmpeg for too long, instrument/introduce subprocess-level cancellation for the *currently running* extraction; do not restore queue-wide generation.
6. If video still fails, collect the exact source container/codec + relevant server log lines and identify which stage failed (direct/HLS, remux, compatibility). Add a targeted fixture before changing transport globally.
7. Keep v2.14.7 sidecar reindex semantics and v2.14.6 Keep Last semantics intact.
8. Run every `validation/validate_*.py`, Python compile, JS syntax, then clean-extracted FULL validation and PATCH runtime-preservation overlay before declaring a later release green.
9. Every development response must include FULL SOURCE ZIP + PATCH ZIP + current HANDOFF Markdown and, when available, email them to `christianpwire@gmail.com`.

---

# Prior complete handoff (v2.14.7 and earlier)
# CTBRec Mobile Reviewer v2.14.7 — Complete Development Handoff

**Release state:** FINAL / GREEN for automated validation. v2.14.6 baseline + stale READY-index / existing-mosaic reindex repair below. The complete v2.14.6-and-earlier handoff is appended after this authoritative section.

## Standing delivery requirement
Every assistant development response for this project, complete or incomplete, must include downloadable artifacts for:
1. the complete current FULL SOURCE ZIP;
2. the current safe PATCH/UPDATE ZIP;
3. a complete current Markdown handoff containing implemented + planned work, validation, known issues, architecture/context, and exact resume steps.

Do not carry forward a stale handoff.

## Why v2.14.7 exists
The user supplied an affected model's `._cb_chunk_reviewer_state.json` and complete `._chunk_mosaics` folder after reporting:
- model tile correctly showed ~22 GB of Original files;
- many mosaics were visibly present in the model folder;
- opening Originals exposed only one mosaic;
- requesting Ahead/Generate now said there were no upcoming mosaics.

### Concrete field evidence from the supplied bundle
Inspection found:
- **25 JPEG mosaic files** in `._chunk_mosaics`;
- **16 `.sources.json` sidecars**;
- those 16 sidecars reference **103 source recordings totaling ~22.454 GiB**, essentially the same size as the model tile;
- **9 extra JPEGs are orphan legacy duplicates** with older chunk numbers but the same start timestamps as newer sidecar-backed mosaics;
- the supplied `._cb_chunk_reviewer_state.json` marks only `2026-03-12T18:27:59|2026-03-12T18:50:41|n=2` as `done`;
- the sidecar-backed mosaics span Jan 31 through Mar 15 and are internally consistent: recorded JPEG dimensions/tile rectangles match the actual uploaded JPEG files.

This proves the user's core media/mosaic data was not missing. The UI queue was stale.

## Root cause
`mobile_ready_work_index.json` is a **derived cache**, but `quick_load_queue()` treated it too much like the authoritative source of existing ready mosaics.

If that cache contained one ready row, phone open did this:
1. read that one row;
2. create a queue containing one chunk;
3. show one mosaic;
4. asynchronously start the sort-first worker.

The durable `._chunk_mosaics/*.sources.json` metadata already on disk was not reindexed synchronously unless the ready-index had no usable work at all.

Manual **Ahead / Generate now** then compounded the problem: `prefetch_task()` sliced only the chunks already present in the open queue. A one-chunk queue therefore yielded an empty `chunks[1:N]` list and reported `0/0 upcoming`, even if many trustworthy sidecar-backed mosaics were sitting on disk.

This is why the physical folder, catalog size and mobile sorter could disagree.

## Implemented v2.14.7 changes

### 1. Sidecar inventory is checked on model open
Added `MobileReviewerState._existing_mosaic_sidecar_count(mode, model_name, selected_drives)`.

For Originals it counts `._chunk_mosaics/*.sources.json`; for Review it counts the corresponding Review manifest files. This is only a cheap inventory signal.

### 2. READY index self-heals from disk metadata
`quick_load_queue()` now treats the ready index as a cache.

If:
- no usable ready chunks are cached, **or**
- the model has more durable sidecars on disk than the number of cached ready chunks,

Reviewer calls `_recover_partial_snapshot_from_existing_mosaics()` before opening the queue.

That recovery path:
- reads sidecar JSON;
- confirms listed source files still exist;
- confirms source size/mtime still match;
- confirms listed mosaic outputs exist;
- validates the embedded mobile hit map;
- reconstructs the ready snapshot;
- does **not** invoke ffprobe, ffmpeg, frame extraction, Recu, or full mosaic regeneration.

For a model like the supplied one, this converts a stale `1 ready` cache into the full valid sidecar-backed ready subset immediately.

### 3. Reviewed/skipped Originals are not resurrected
Original sidecar recovery now loads `._cb_chunk_reviewer_state.json` and ignores any sidecar whose `chunk_key` is explicitly `done` or `skipped`.

This is critical because recovery must repair cache state without undoing the user's sorting decisions.

### 4. Manual Ahead now reindexes existing mosaics first
`prefetch_task()` now performs partial sidecar recovery and merges the recovered chunks into the live queue **before** slicing upcoming work.

Therefore a stale one-chunk queue can no longer say there are no future mosaics when valid sidecar-backed mosaics already exist.

If metadata reconciliation is still in progress and the queue genuinely does not yet know all chunks, the status now says that remaining chunk metadata is being reconciled rather than falsely claiming `0/0 upcoming`.

### 5. Recovery feedback is visible on the phone
Successful quick-open responses include:
- `recovered_existing_mosaics`
- `sidecar_count`
- `source: "sidecar-reindex"` when the repair path was used.

The phone open toast reports when additional existing mosaics were recovered from disk metadata.

### 6. Orphan JPEG policy remains conservative
The supplied bundle contains 9 legacy JPEGs with no trustworthy sidecar. v2.14.7 does **not** blindly surface those as sortable work because a JPEG without source identity + hit-map metadata is exactly how mismatched tap-box bugs can recur.

Those orphan JPEGs are ignored unless they can be reconciled/regenerated through the existing v2.14.4 **Rebuild chunk + mosaic** flow.

Do not weaken this rule merely to make every image file visible.

### 7. Versioning
- server: `CTBRecMobile/2.14.7`
- PWA cache: `ctbrec-shell-v2147`
- static asset query version: `2147`

## Validation
New suite: `validation/validate_v2_14_7_sidecar_reindex.py`.

It constructs a model with:
- 3 durable sidecars;
- only 1 row in the derived ready-index;
- 1 of the sidecar chunks explicitly marked reviewed/done;
- 1 orphan JPEG without a sidecar.

Expected/verified behavior:
- sidecar inventory sees 3 metadata files;
- quick-open self-heals from stale ready-index;
- only 2 chunks are surfaced (the reviewed chunk is not resurrected);
- recovery reports 1 additional existing mosaic;
- orphan JPEG is ignored;
- Ahead reindexes before determining future work and does not report bogus `0/0 upcoming`.

Full validation pass also reran all prior suites:
- mobile transport/recovery;
- 4,100-model DOM/race tests;
- READY preservation;
- settings overwrite guard;
- EZ Sort persistence;
- Original/Review FFmpeg batching;
- NSFW batching;
- Recu incremental + capture/fallback suites;
- direct-disk preview;
- scroll-to-top;
- v2.14.4 mosaic integrity/rebuild;
- v2.14.5 tagged filename parser;
- v2.14.6 cross-drive per-session Keep Last.

All passed in the working tree before packaging. The preliminary FULL SOURCE ZIP was then extracted into a clean directory and the complete validation suite passed again. The PATCH was overlaid onto a sanitized v2.14.6 baseline; representative runtime files remained byte-for-byte unchanged and the v2.14.7 recovery regression passed from the overlaid tree.

## Important behavior preserved from v2.14.6
Keep Last semantics remain:
- combine a model across all drives;
- timestamp parser accepts tagged names;
- split into sessions only by >configured uncovered gap (default 30 minutes);
- apply Keep Last independently to the tail of every session;
- retain whole files at the boundary;
- drive changes do not create sessions.

Do not revert this behavior unless the user explicitly changes the requirement.

## Known limitations / future work
1. Sidecar reindex can only recover mosaics whose sidecar source descriptors still match actual files. If files were renamed/modified after mosaic creation and identity cannot be reconciled safely, the mosaic is rejected and should be rebuilt.
2. Orphan JPEGs without trustworthy source/hit-map metadata are intentionally ignored. A future optional model-level maintenance action could list/delete proven duplicate orphans, but it must not guess.
3. Sidecar counting adds a small amount of disk metadata work on model open only when needed. If extremely large models make this noticeable, add a directory-inventory cache keyed by mosaic-directory stat metadata rather than removing the correctness check.
4. If the user reports a model still showing fewer mosaics after v2.14.7, request its current `._chunk_mosaics` directory + `._cb_chunk_reviewer_state.json` + `mobile_ready_work_index.json`/diagnostics ZIP and compare exact sidecar signatures/source identities.

## Packaging / upgrade safety
PATCH must exclude active/private runtime state, including:
- `mobile_config.json`
- `recording_roots.txt`
- `mobile_action_queue.json`
- catalog/ready/background state
- Recu session/cache/profile
- model metadata
- media files
- NudeNet model binaries unless specifically required.

FULL SOURCE is the sanitized v2.14.6 full source plus v2.14.7 code/docs/tests.

## Exact next steps for another AI agent
1. Use the v2.14.7 FULL SOURCE ZIP as the baseline.
2. Preserve sidecar-reindex semantics: ready index is derived cache; sidecars + current file identity are durable truth for already-built mosaics.
3. Never resurrect chunk keys marked `done`/`skipped`.
4. Never surface orphan JPEGs as clickable mosaics without trustworthy hit-map/source metadata.
5. If more queue problems appear, inspect both the ready snapshot **and** actual sidecars before changing generation logic.
6. Keep manual Ahead capable of reconciling disk metadata before deciding no future work exists.
7. Preserve v2.14.6 Keep Last semantics and all earlier destructive-safety invariants.
8. On every development response, provide FULL SOURCE ZIP, PATCH ZIP, and current HANDOFF Markdown.

---

# Prior complete handoff (v2.14.6 and earlier)
# CTBRec Mobile Reviewer v2.14.6 — Complete Development Handoff

**Release state:** FINAL / GREEN for automated validation. v2.14.5 baseline + the Keep Last session-scope correction below. The complete v2.14.5-and-earlier handoff is appended after this authoritative section.

## Why v2.14.6 exists
The user explicitly clarified that v2.14.5 changed Keep Last in the wrong direction. The desired behavior is **not** one model-wide newest-duration allowance. Instead, Keep Last is intended to sample the tail of every distinct recording session because separate sessions may contain materially different content.

A session is defined by time continuity, not by physical drive. CTBRec may switch drives in the middle of one stream, so E:/F:/G:/C: boundaries must not create extra sessions. A new session begins only when there is **more than** the configured Keep Last gap (default 30 minutes) of uncovered time between the end of known recording coverage and the next recording start.

## Required Keep Last semantics
For one model with a `15` minute rule:

- combine eligible complete recordings from every configured recording root;
- parse the recording timestamp wherever it appears in the filename (v2.14.5 parser remains);
- sort chronologically across drives;
- use known/estimated duration to determine covered time;
- split into sessions only when the next recording begins >30 minutes after covered footage ends (or the user-configured gap);
- retain whole newest files from the tail of **each session** until that session reaches at least 15 retained minutes;
- send older complete files from each session to the normal durable deletion plan;
- recent/in-progress and timestamp-unparseable files remain protected.

Concrete example, 10-minute files and a 15-minute Keep Last rule:

Session 1 (spans E: and F:):
- E:/model/model_2026.09.14_10.00.00_tail_10m00s_est.mp4
- F:/model/model_2026.09.14_10.12.00_tail_10m00s_est.mp4
- E:/model/model_2026.09.14_10.24.00_tail_10m00s_est.mp4

Session 2 (starts after >30 minutes of uncovered time):
- F:/model/model_2026.09.14_12.00.00_tail_10m00s_est.mp4
- E:/model/model_2026.09.14_12.12.00_tail_10m00s_est.mp4

Expected result:
- Session 1: keep 10:12 + 10:24 (20 minutes because retention is whole-file); delete 10:00.
- Session 2: keep 12:00 + 12:12 (20 minutes); delete nothing because the entire session is needed to reach/cover 15 minutes.

This intentionally means a sparse model with four genuinely separate 10-minute sessions can retain all four files under a 15-minute rule. That is now confirmed as the user's intended behavior.

## Implemented v2.14.6 changes

### 1. Reverted v2.14.5 model-wide selection scope
`ctbrec_keep_last.py` no longer consumes one retention allowance across the entire model. It now applies the configured duration independently to every session.

### 2. Cross-drive session grouping
All rows for a model are merged before session detection. Physical folders/drives do not split sessions. This preserves one continuous session even when CTBRec changes output drives mid-stream.

### 3. Gap uses uncovered footage, not start-to-start distance
The planner tracks `coverage_end` using recording duration. A new session starts only when:

`next_start - coverage_end > gap_minutes`

Exactly 30 minutes with a 30-minute gap setting remains the same session; 30 minutes + epsilon becomes a new session.

This is safer than the older v2.14.4 start-to-start check, which could split a long recording merely because the next file's timestamp was >30 minutes after the prior file's start.

### 4. Segment-aware coverage
If multiple `_segment_N` files share one base recording timestamp, their durations extend synthetic covered time consecutively for session-gap detection. This avoids splitting a session because segmented files reuse one base timestamp.

### 5. v2.14.5 filename parsing retained
Do **not** revert the tagged filename parser. Keep Last still recognizes dotted, dashed, and compact CTBRec timestamps anywhere in names containing `_tail_...`, `_endminus_...`, `_segment_...`, etc.

### 6. UI wording corrected
Settings now explains that Keep Last combines recordings across drives, splits by session gap, and retains the tail of every session. The confirmation prompt says the same. The setting label is now `New-session gap (min)`.

### 7. Versioning
- server: `CTBRecMobile/2.14.6`
- PWA cache: `ctbrec-shell-v2146`
- static asset query version: `2146`

## Behavior intentionally unchanged
- Keep Last still retains **whole complete files**. It does not silently trim a boundary file to exactly N minutes.
- Retained Keep Last files go through the existing durable PC action queue and normal Review destination behavior.
- Older complete files are planned for `MARKED_FOR_DELETION` using existing safety semantics.
- Recent/in-progress files remain protected.
- Timestamp-unparseable files remain protected.
- v2.14.4 end-relative retained-file naming remains intact.
- Recu, mosaic integrity/rebuild, model sizing, live bridge, frame cuts, background scheduling, and all other v2.14.5 functionality are preserved.

## Validation added/updated
New suite: `validation/validate_v2_14_6_keep_last_sessions.py`.

It verifies:
- tagged timestamp parser remains intact;
- one session can span multiple physical drives;
- a 15-minute rule is independently applied to two separate sessions;
- exactly 30 minutes uncovered gap stays in one session;
- >30 minutes creates a new session;
- same-base `_segment_N` files extend covered time consecutively.

The historical `validate_v2_14_5_keep_last.py` now protects only the filename parser/duration behavior because its model-wide selection assertion was explicitly superseded by the user.

All validation scripts under `validation/` passed on the v2.14.6 working tree before packaging.

Final-package validation also passed: the FULL SOURCE ZIP was extracted cleanly and every validation suite reran successfully; the PATCH was confirmed to exclude private/runtime state; and overlaying the PATCH onto a v2.14.5 baseline preserved representative runtime/config files byte-for-byte while the v2.14.6 Keep Last regression still passed.

## Packaging / upgrade safety
PATCH must contain only program/docs/test files and must not include active runtime state such as `mobile_config.json`, `recording_roots.txt`, queues, catalog state, Recu session/profile, or model binaries unless specifically required. FULL SOURCE is based on the existing sanitized v2.14.5 full-source package plus v2.14.6 code/docs/tests.

## Exact next steps for another AI agent
1. Use the v2.14.6 FULL SOURCE ZIP as the baseline.
2. Do not restore v2.14.5 model-wide Keep Last behavior unless the user explicitly changes their mind again.
3. Preserve the cross-drive session rule: only time continuity defines sessions.
4. If real-world Keep Last output is still surprising, request the actual filenames/durations and inspect the planned session boundaries before altering destructive logic.
5. If the user later requests exact-N-minute trimming, implement it as a separate explicit durable FFmpeg cut workflow reusing verify-before-original-deletion safety; do not silently change whole-file semantics.
6. On every development response, provide the complete current FULL SOURCE ZIP, current PATCH ZIP, and complete current HANDOFF Markdown.

---

# Prior complete handoff (v2.14.5 and earlier)
# CTBRec Mobile Reviewer v2.14.5 — Complete Development Handoff

**Release state:** FINAL / GREEN. v2.14.4 baseline + the Keep Last corrections below. Full prior v2.14.4-and-earlier handoff is appended verbatim after this authoritative v2.14.5 section.

## Standing delivery requirement
Every assistant development response for this project, complete or incomplete, must include downloadable artifacts for: (1) the complete current full-source ZIP, (2) the current safe patch/update ZIP, and (3) a complete current Markdown handoff containing implemented + planned work, validation, known issues, architecture/context, and exact resume steps.

## Why v2.14.5 exists
The user asked whether Keep Last was safe with tagged filenames where the CTBRec recording timestamp can appear in the middle of the filename, for example around `_tail_<minutes>m<seconds>s_est` metadata. They also reported that an older build's Keep Last run moved effectively everything for one model to Review instead of only retaining the requested newest duration.

Inspection of the v2.14.4 source found two distinct facts:
1. Dotted timestamps were already searched anywhere in the filename, but parsing was inconsistent across timestamp formats and segment placement; compact timestamps created by newer output naming were not supported, and filename-duration metadata support was incomplete.
2. More importantly, the Keep Last planner applied the requested retention duration independently to every >`gap_minutes` block and independently to every physical duplicate model folder/root. Sparse recordings can naturally be >30 minutes apart, so each file could become its own block and therefore each block kept at least one file. Multiple drives multiplied that behavior again. This exactly explains a run moving nearly everything to Review even though timestamp parsing itself succeeded.

## Implemented v2.14.5 changes

### 1. Position-independent tagged filename timestamp parser
`ctbrec_keep_last.py::parse_time()` now recognizes timestamps anywhere in the filename:
- dotted: `YYYY.MM.DD_HH.MM.SS`
- dashed: `YYYY-MM-DD_HH-MM-SS`
- compact: `YYYYMMDD_HHMMSS`

Tags may appear before or after the timestamp. Segment markers are parsed separately, so examples such as all of the following are supported:
- `model_2026.09.14_12.34.56_tail_15m03s_est.mp4`
- `model_tail_15m03s_est_2026.09.14_12.34.56.mp4`
- `model_2026-09-14_12-34-56_tail_15m03s_EST.ts`
- `model_20260914_123456_endminus_15m00s_to_0m00s.mp4`
- `model_2026.09.14_12.34.56_tail_15m03s_est_segment_7.ts`
- `model_segment_8_tail_15m03s_est_2026.09.14_12.34.56.ts`

No mtime fallback was added. Unparseable names remain protected by design.

### 2. Richer tagged duration parsing
`labelled_duration()` now supports:
- `_tail_15m03s_est`
- optional hours such as `_tail_1h2m03s_est`
- case-insensitive `_EST`
- v2.14.4+ `_endminus_15m00s_to_0m00s` metadata, whose endpoint difference is used as retained-duration metadata when exact cached duration is unavailable.

Exact duration cache still has priority when present.

### 3. Keep Last is now model-wide, not block-wide/root-wide
For each model rule, all complete eligible files found under that model across every configured recording root are considered together. They are sorted newest-first by recording timestamp, then segment number for same-base segmented files. Whole files are retained until cumulative retained duration reaches the requested target. Remaining older files are planned for `MARKED_FOR_DELETION`.

The old `gap_minutes` setting remains accepted for backward configuration compatibility but no longer multiplies retention targets across gap blocks.

This also means duplicate physical model folders across drives do not each get their own independent `X minutes` retention allowance.

### 4. Safety invariants preserved
- recent/in-progress writes are excluded/protected;
- timestamp-unparseable recordings are excluded/protected;
- no mtime-based destructive guess;
- Keep Last actions still execute through the durable PC action queue;
- retained files still move into the Review folder belonging to their source model folder;
- deletion destinations are still drive-aware `MARKED_FOR_DELETION/<model>`;
- missing sources at execution time are skipped safely.

### 5. Important semantic limitation retained intentionally
Keep Last remains **whole-complete-file retention**, matching prior behavior. It does not silently trim the boundary recording. Therefore, if a rule is 15 minutes but the newest complete source file is 2 hours long, Keep Last retains that one 2-hour file rather than transcoding/cutting it to exactly 15 minutes. Any future exact-boundary trim should reuse the verified v2.12 frame-cut/FFmpeg transactional safety model and must be an explicit product change, not silently introduced.

## Validation
New suite: `validation/validate_v2_14_5_keep_last.py`.
It covers all filename forms above, tail/endminus duration parsing, segment parsing independent of tag order, and the previously broken sparse/multi-root retention case. Four 10-minute recordings spaced >30 minutes apart across two roots with a 15-minute rule correctly keep only the newest two and delete-plan the older two.

Every pre-existing validation script also passes, including v2.13 mobile/browser/transport, v2.14 Recu, desktop preview, scroll reset, and v2.14.4 traversal/mosaic integrity.

## Final packaging gates
- clean-extracted FULL archive: every `validation/*.py` suite PASS; JS syntax PASS; fresh config contains blank PIN/secret.
- v2.14.4→v2.14.5 PATCH overlay: representative runtime/config files remained byte-identical; changed code compiled; Keep Last + mosaic-integrity regressions PASS.
- PATCH contains no active runtime config, roots, queue, catalog, Recu session/profile, or media.

## Versioning
- server: `CTBRecMobile/2.14.5`
- PWA shell cache: `v2145`
- update notes: `UPDATE_FROM_2_14_4.txt`
- changelog: `CHANGELOG_v2_14_5.txt`
- validation report: `TEST_REPORT_v2_14_5.txt`

## Known issues / planned work
- Field-test Keep Last against the user's real tagged filenames and multiple recording roots.
- If the user wants *exactly* N minutes rather than whole-file granularity, design a separate durable Keep Last boundary-cut job using the existing verify-before-original-deletion frame-cut pipeline. Do not repurpose the current whole-file move semantics without explicit confirmation.
- Preserve all v2.14.4 traversal, mosaic rebuild/integrity, end-relative naming, v2.14.3 Recu explicit capture, v2.13 reliability/performance, and earlier destructive-safety behavior.

## Exact next steps for another AI agent
1. Use v2.14.5 FULL SOURCE as baseline.
2. If a field report says Keep Last still retained unexpected files, request/upload filenames or a diagnostic listing; inspect `planned_review_files`, `planned_delete_files`, and `plan_stats` in the durable Keep Last job before changing destructive logic.
3. Re-run `validation/validate_v2_14_5_keep_last.py` plus every `validation/*.py` after changes.
4. Always output full source ZIP + patch ZIP + updated Markdown handoff in every development response.

---

# PRIOR v2.14.4 HANDOFF (preserved verbatim)

# CTBRec Mobile Reviewer v2.14.4 — Complete Development Handoff

**Release state:** **FINAL / GREEN**. Source, clean-extracted FULL archive, and v2.14.3→v2.14.4 PATCH overlay validation all passed.

**Authoritative baseline:** v2.14.3 + the v2.14.4 changes below. The full prior v2.14.3-and-earlier handoff is appended after this section so another AI can resume without needing any other conversation.

## Non-negotiable delivery rule

For every development response, complete or incomplete, always provide:
1. the complete current **FULL SOURCE ZIP**;
2. the current **PATCH/UPDATE ZIP**;
3. the complete current **HANDOFF Markdown** containing implemented work, planned work, tests, known issues, architecture/context, and exact next steps.

Never replace user runtime state in the patch.

# 1. Why v2.14.4 exists

The user reported four classes of mosaic-workflow problems plus one naming request:

1. The unattended whole-library mosaic pass was failing to reach ~4,000 models and appeared to restart from model 1 before completion.
2. Multiple Settings fields imposed arbitrary maxima, so the user could not enter longer intervals/counts.
3. Model-list tiles did not reliably refresh displayed bytes/size after disk/catalog scans.
4. There were still duplicate mosaics for the same source recordings and mosaics whose interactive tap boxes did not match the visual tiles (extra/missing/misaligned boxes).
5. User requested a force-rebuild button that first reconciles the chunk against current disk contents, then deletes stale mosaics and makes exactly one new authoritative mosaic/hit map.
6. Newly kept media should encode its position in the chunk, preferably measured backward from the chunk end for future Keep Last decisions.

# 2. Root-cause findings

## 2.1 Full-library pass reset

`_library_plan_fingerprint()` used to include **mutable model byte totals and drive metadata**. `_run_library_generation()` rebuilds the largest-first catalog plan whenever the worker is resumed/restarted. Active recordings and catalog rescans routinely change byte totals. The new plan therefore got a different fingerprint, `_library_resume_cursor()` treated it as a different traversal, and the saved model cursor was discarded.

This is the main deterministic code-level explanation for “it was at model N, then started again at model 1.” It is distinct from the repeat-pass interval.

## 2.2 Settings maxima

Both `static/index.html` and `update_mobile_settings()` imposed arbitrary upper bounds on operational settings such as idle/rescan minutes, upcoming mosaics, mosaic frames/columns/tile width, NSFW operational fields, offline pack sizes, etc. Some runtime paths also re-clamped those values.

## 2.3 Stale model sizes

PC catalog scans correctly replace `self.catalog`, update `catalog_status.updated_at`, and invalidate server catalog caches. But the phone's `refreshBackgroundStatus()` only refreshed model cards after catalog changes for the **Deletion** tab. Original/Review/EZ Sort/Hidden could keep stale client cache rows and thus stale byte totals.

## 2.4 Duplicate mosaics / tap-map mismatch

Several independent weaknesses could compound:

- Original mosaic generation could leave orphan `_partXXofYY.jpg` files from a previous generation when the new generation had a different part count.
- Chunk indices/base filenames can shift as recordings are added, while signature/source-equivalent sidecars from prior generations remain on disk.
- READY snapshots could contain duplicate logical chunks representing the same exact source set.
- Existing `mobile_layout_v2` validation checked image dimensions and box bounds but did not fully validate the tap-target count/order/timestamps against the actual generation plan.

# 3. Implemented v2.14.4 changes

## 3.1 Durable full-library traversal snapshot

New runtime file:

`mobile_library_mosaic_plan.json`

New helpers in `ctbrec_mobile_server.py`:

- `_load_persisted_library_plan()`
- `_persist_library_plan()`
- `_clear_library_plan()`

A full-library pass now creates one ordered largest-first `{mode,name,bytes,drives}` snapshot and retains it across `running`, `queued`, `waiting`, `paused`, and `error` states. The pass fingerprint is computed from stable ordered `{mode,name}` identity only; changing bytes/drives does **not** reset the pass.

When the pass completes, the durable plan file is removed. The next pass rebuilds size ordering and includes new models. This deliberately favors liveness/completion over reordering an in-progress pass every time active recordings grow.

The previous artificial 15-minute floor in library/NSFW repeat scheduling was also removed; a user-provided non-negative interval is respected.

## 3.2 Arbitrary operational maxima removed

HTML `max=` attributes and corresponding backend/runtime upper clamps were removed for operational settings, including:

- whole-library idle/rescan;
- frame-cut idle delay;
- Keep Last gap/grace;
- NSFW idle/rescan/sample interval/batch/columns/tile width/max tiles/grace;
- Original/Review background upcoming counts and idle delays;
- Original/Review mosaic spacing/columns/tile width/max frames;
- speed-mode suggestion/offline pack sizes;
- offline pack model/chunk count and MB limit.

The hidden `open_fast_initial_chunks` validation budget no longer has an arbitrary max 12 either.

Intrinsic/semantic bounds are intentionally retained where values are physically/protocol constrained: TCP ports, 0–1-ish confidence values, JPEG quality, and a few internal implementation guards such as FFmpeg extraction batch size. These are not the user-facing “how long/how many” caps that caused the complaint.

## 3.3 Live model-size refresh

`static/app.js` now tracks `lastCatalogUpdatedAt` globally. When `/api/background-status` reports a new catalog timestamp:

- phone-side model-list cache is cleared;
- if a model-list view is currently visible, `loadModels({force:true, resetWindow:false})` runs;
- the current progressive rendering/scroll window is preserved.

This applies to Original, Review, EZ Sort, Hidden, etc., not only Deletion.

## 3.4 Original mosaic canonical-set cleanup

`ctbrec_mosaic_sort_lite.py::generate_mosaic()` now removes same-base stale:

- single JPEG;
- all `_part*of*.jpg` JPEGs;
- `.sources.json`

whenever it is actually going to generate a fresh canonical set, not just in one force branch. This prevents old part-count artifacts from surviving next to a new set.

Original sidecars now include per-source duration and write `mobile_layout_v2.version = 3`. Each tile stores exact:

- `file_index` / `file_number`;
- `frame_index`;
- `local_seconds`;
- pixel rectangle.

## 3.5 Strict hit-map validation

`_embedded_layout_entry()` now rejects layouts if any of the following fail:

- sidecar sources cannot align to current chunk files;
- sidecar mode/signature/output list mismatches;
- actual JPEG dimensions mismatch recorded dimensions;
- any box is outside the actual image;
- Original v3 frame file-index sequence differs from a reconstructed `build_frame_plan()`;
- Original v3 `local_seconds` sequence differs from the expected plan;
- Review v3 `frame_index` sequence is not exactly `0..N-1`;
- duplicate pixel rectangles exist within one image.

Invalid layouts are regenerated instead of being exposed to the phone.

## 3.6 Source-identity stale-mosaic purge

New helper:

`_purge_mosaic_artifacts_for_chunk_sources(mode, chunk)`

It scans sidecars in that model's mosaic directory and removes artifact sets whose signature matches **or** whose normalized source-file set exactly matches the chunk. This handles cases where chunk/base indices shifted but the same recordings were represented by multiple mosaics.

`ensure_chunk_mosaic()` invokes this purge before forced regeneration and before repairing an existing mosaic whose layout failed validation.

## 3.7 READY/queue duplicate suppression

`quick_load_queue()` now de-duplicates rows by both:

- exact chunk signature;
- exact normalized source-file tuple.

This prevents two durable READY rows representing the same recording set from both appearing in the phone queue.

## 3.8 Rebuild chunk + mosaic

UI now has two distinct actions:

- **Redo mosaic**: recreate the current mosaic for the existing chunk definition.
- **Rebuild chunk + mosaic**: reconcile the chunk from current disk files first.

New endpoint:

`POST /api/queues/{queue_id}/rebuild-chunk`

New state task:

`rebuild_current_chunk_task(queue_id)`

Behavior:

1. Capture current chunk/source set.
2. Re-scan the current model folder(s) from disk with the same chunk builder used by the library worker.
3. Match the fresh chunk primarily by exact-source overlap, then temporal overlap, then source bytes.
4. Explicitly determine prior expected files that are now missing and newly discovered files that now belong to the chunk.
5. Purge stale/duplicate mosaic artifacts tied to the old and fresh source sets.
6. Replace the queue head with the fresh reconciled chunk.
7. Drop queued tail chunks whose source set is identical to or wholly duplicated by the rebuilt chunk.
8. Clear the old draft.
9. Force one new authoritative mosaic + hit map.
10. Persist the reconciled full-model READY snapshot and invalidate catalog views.
11. Return counts for current files, added files, missing files, and removed artifacts.

If the old chunk has no meaningful overlap with any current disk chunk, the operation refuses rather than guessing and instructs the user to return to Models/reopen.

## 3.9 End-relative kept-media filename metadata

New canonical tag:

`_endminus_{start_from_end}m{ss}s_to_{end_from_end}m{ss}s`

Example: final 15 minutes of a two-hour chunk:

`_endminus_15m00s_to_0m00s`

Implemented in:

- precise Review frame-cut output names (`build_trim_output_name()`);
- whole Original KEEP moves into Review;
- whole Review segment moves to Cumshots/Misc Hot Scenes.

The durable queue stores `chunk_start_seconds`, `chunk_end_seconds`, and `position_from_end_tag` so destination naming remains deterministic when the job later executes. Existing `_endminus_` names are not double-tagged. Deletes are never renamed solely for this feature. “Leave for review” remains in place and therefore is not renamed.

# 4. Validation added

New suite:

`validation/validate_v2_14_4_mosaic_integrity.py`

It verifies:

- plan fingerprint is unchanged when only bytes/drives change;
- reordered model identity changes the fingerprint;
- durable plan round-trip;
- arbitrary HTML max attributes are absent from operational fields;
- client model-size refresh markers exist;
- rebuild endpoint/button exists;
- strict layout plan/timestamp validation exists;
- final-15-min-of-2h naming contains `_endminus_15m00s_to_0m00s`;
- a **real synthetic two-video Original mosaic** is generated via FFmpeg/Pillow;
- v3 sidecar has one tile per planned frame with exact indices/timestamps/durations;
- forcing a second generation with a different part size removes old multi-part JPEGs instead of leaving duplicates.

All pre-existing validation scripts remain required. `validate_v2_13_1_recovery.py` and `validate_v2_13_http.py` were updated only for the current v2144 shell/server version while preserving the same transport invariants.

# 5. Current safety invariants that must not regress

- Never silently write native CTBRec `models.json`; native runtime mutations use the identity-verified bridge only.
- User sorting must remain responsive while Recu/background generation/NSFW/model-admin work proceeds.
- Destructive media actions are durable/idempotent and originals are never deleted until verified replacement output exists for frame-cut jobs.
- `MARKED_FOR_DELETION` remains reversible until explicit permanent deletion.
- Recu v2.14.3 explicit-confirmation capture semantics remain unchanged in v2.14.4.
- Background frame cuts are idle-only and outrank whole-library mosaics/NSFW once eligible.
- Active/visible phone mosaic demand outranks background work.
- Old/historical phone queues cannot indefinitely block the full-library worker.
- A pathological model is retried a bounded number of times and then skipped for that pass.

# 6. Runtime/upgrade files — never ship in PATCH

Do not overwrite user state such as:

- `mobile_config.json`
- `recording_roots.txt`
- `mobile_action_queue.json`
- `mobile_catalog_cache.json`
- `mobile_ready_work_index.json`
- `mobile_library_mosaic_state.json`
- **new** `mobile_library_mosaic_plan.json`
- `mobile_non_nsfw_state.json` / indexes
- `mobile_model_metadata.json`, hidden/sidecar metadata
- Recu session/cache/profile files
- existing mosaic caches
- NudeNet ONNX model in an update patch.

The FULL SOURCE package may contain the normal sanitized starter config/roots and bundled model as in prior releases; it must not contain private runtime data from a live install.

# 7. Field-test checklist

After overlaying v2.14.4:

1. Start/restart a whole-library background pass and watch the model number advance. Let a catalog scan occur or pause/resume the worker; confirm it resumes the saved model index instead of resetting to 1.
2. Enter a value above the old max (for example a very long repeat interval) and save/reopen Settings; confirm exact value persists.
3. Run a catalog scan while viewing All Models/EZ Sort and confirm model tile byte totals update without leaving/re-entering the tab.
4. On a chunk with suspect duplicate/misaligned mosaic, use **Rebuild chunk + mosaic**. Confirm it reports current/added/missing source counts and the new mosaic has exactly one tap target per visual tile.
5. Keep a late-in-chunk Original file or precise Review range and confirm the output filename includes `_endminus_...`.
6. Verify old v2.14.3 Recu capture still works exactly as before.

# 8. Planned/future improvements (not required for v2.14.4)

- Add an optional maintenance audit that scans all existing mosaic sidecars offline and reports/quarantines source-equivalent duplicate sets without waiting for a model to be opened.
- Optionally expose a user-facing “current pass snapshot created at” and “new models waiting for next pass” count.
- Consider a one-click “rebuild every invalid mosaic for this model” command, but keep it background/idle-only.
- Further profile catalog generation if 4,000-model list requests regress above ~1s; do not reintroduce risky mobile transport changes.
- Field-test Recu v2.14.3 semantics before changing its authentication architecture again.


## Final v2.14.4 packaging gate — PASS

- Clean-extracted FULL archive: all 9 top-level Python modules compiled; `static/app.js` and `static/rapid.js` passed syntax checks; every `validation/*.py` suite passed from the extracted archive.
- PATCH overlay onto a seeded v2.14.3 install: 14 representative user/runtime/private files remained byte-for-byte unchanged, including config, roots, queues, catalog/READY/library state, the new traversal-plan sentinel, NSFW state, model/hidden metadata, Recu session/cache/profile sentinel, and NudeNet model.
- Overlayed code compiled and the v2.14.4 integrity suite passed.
- `mobile_self_test.py` itself is dependency-sensitive: the Linux build container lacks NudeNet, so its NudeNet installation check cannot pass there; the user's Windows install previously reports NudeNet installed. This is not a code-validation failure.

# 9. Packaging requirement for this release

Expected files:

- `CTBRec_Mobile_Reviewer_v2_14_4_FULL_SOURCE.zip`
- `CTBRec_Mobile_Reviewer_v2_14_4_PATCH.zip`
- `CTBRec_Mobile_Reviewer_v2_14_4_HANDOFF.md`
- `TEST_REPORT_v2_14_4.txt`
- `SHA256SUMS_v2_14_4.txt`

Patch code/docs should include at least:

- `ctbrec_mobile_server.py`
- `ctbrec_mosaic_sort_lite.py`
- `ctbrec_review_folder_sort_lite.py`
- `mobile_self_test.py`
- `static/index.html`
- `static/app.js`
- `static/service-worker.js`
- `README.md`
- `CHANGELOG_v2_14_4.txt`
- `UPDATE_FROM_2_14_3.txt`
- this handoff
- v2.14.4 validation/report.

# 10. Exact resume instructions for another AI

If this work is interrupted before final packaging:

1. Use the v2.14.4 working/full source, **not v2.14.3**, as the new baseline.
2. Run Python compilation and JS syntax checks.
3. Run every `validation/*.py`, especially `validate_v2_14_4_mosaic_integrity.py`.
4. Run `mobile_self_test.py` on a Windows environment with NudeNet installed; a Linux dev environment without NudeNet may fail only that dependency check even when code is valid.
5. Build PATCH without runtime/user-state files.
6. Overlay PATCH onto a seeded v2.14.3 install and hash-check all sentinel runtime files before/after.
7. Extract the final FULL archive into a clean directory and rerun compilation + validation from the extracted archive.
8. Generate test report/checksums.
9. Update this section's release state from FINAL CANDIDATE to FINAL / GREEN only after those packaging gates pass.

---

# Complete prior handoff (v2.14.3 and earlier)

# CTBRec Mobile Reviewer v2.14.3 — Complete Development Handoff

**Status:** FINAL / GREEN for automated validation. Real-PC Recu field validation remains the release gate.

**Current baseline:** v2.14.2 + v2.14.3 explicit-confirmation Recu verification fix.

This v2.14.3 section is authoritative. The complete v2.14.2 and earlier handoff is appended below for continuity.

## Standing packaging requirement
Every future development response, complete or incomplete, MUST include: (1) complete current full-source ZIP, (2) current safe patch/update ZIP, and (3) a fully current Markdown handoff containing implemented changes, planned work, validation, known issues, architecture, exact next steps, and all information needed for a new AI agent to resume.

# 1. FIELD FAILURE THAT CREATED v2.14.3

User installed v2.14.2 and supplied:
- screenshot showing the dedicated Chrome window visibly on the ordinary Recu homepage (`recu.me`) with normal site content, **Sign In / Create Account**, `The Biggest Chaturbate Archive`, and `Most Bookmarked Recordings`;
- `mobile_reviewer(1).log`;
- `CTBRec_Mobile_Reviewer_Diagnostics_20260911_222001.zip`;
- the known-working reference package `recu_clip_model_counter_v2_4_1_MOBILE_REVIEWER_SHARED_PROFILE_FULL(1).zip`.

Despite the normal visible Recu page, `POST /api/recu/capture` returned HTTP 400 repeatedly at 22:18:57, 22:19:01, 22:19:11, and 22:19:29. Earlier in the same log, Recu browser fallback repeatedly raised `RecuReauthRequired` even though the user had visibly passed the human-check page.

## Root cause
The v2.14.2 Capture path still inspected the currently open Recu DOM and called `_looks_like_challenge()`. Reviewer had expanded the challenge heuristic beyond the known-working Recu counter to include generic strings such as:
- `attention required`
- `cloudflare ray id`
- `challenge-platform`

Normal accessible Recu pages can include Cloudflare helper/script/footer markup containing those strings. The field screenshot therefore contradicted Reviewer's DOM classification: the browser was usable, while the code classified it as verification.

Reviewer also treated generic Sign In/password markup as evidence of login failure, although Recu's public pages normally show Sign In controls. This conflated **Cloudflare verification** with **Recu account authentication**. The public model/kink pages do not need to be treated as failed verification merely because the header offers Sign In.

# 2. REFERENCE METHODOLOGY THAT MUST BE PRESERVED

The uploaded `recu_clip_model_counter_v2_4_1_MOBILE_REVIEWER_SHARED_PROFILE_FULL(1).zip` was inspected and is the reference for Recu verification semantics. Key properties:

1. Uses the exact persistent Mobile Reviewer `recu_browser_profile` and configured CDP port.
2. Human verification is explicit. The user visually confirms the real page, then presses ENTER.
3. Session capture uses `Browser.getVersion` + `Storage.getCookies`.
4. The working challenge-token set is exactly:
   - `cf-chl-`
   - `verify you are human`
   - `just a moment`
   - `checking your browser`
5. It deliberately says **no automatic DOM-success heuristic is used**.
6. After browser verification, raw HTTP is only an optional speed optimization. If raw HTTP fails, the verified Chrome session is the reliable fallback.
7. Browser scraping uses `Page.navigate` + `document.documentElement.outerHTML`.
8. Performance path is concurrent HTTP when accepted; fallback is the verified Chrome browser session.

v2.14.3 adapts the same philosophy to the Mobile Reviewer UI: clicking **Capture current session** is the explicit user confirmation equivalent of pressing ENTER.

# 3. v2.14.3 IMPLEMENTED CHANGES

## 3.1 Capture no longer guesses verification state
`RecuBrowserBridge.capture_session()` now does only the mandatory capture work:
- confirm CDP browser is running;
- `Browser.getVersion`;
- `Storage.getCookies`;
- retain Recu-domain cookies when present;
- persist exact Cookie + User-Agent + capture timestamp;
- mark the session `validation_transport=explicit_user_confirmation`;
- choose `cookie_http` when cookies exist, otherwise `navigation`;
- apply the session immediately.

Capture does **not**:
- inspect current page HTML;
- call `_page_target()`;
- call `Runtime.evaluate`;
- call `Page.navigate`;
- run a mandatory raw-HTTP probe;
- reject the user's visible page because of hidden login/Cloudflare markup.

This removes the exact 400 loop seen in the field.

## 3.2 Challenge detector narrowed to the field-proven four-token set
`RECU_CHALLENGE_TOKENS` is now exactly the four tokens listed above. Generic Cloudflare helper strings are intentionally excluded. `_challenge_marker()` exposes the exact matching token for diagnostics.

## 3.3 Sign In is not verification failure
`_looks_like_login()` no longer examines generic `Sign In`, `Log In`, or password form markup. It only returns true when navigation actually lands on a dedicated login-route path such as `/login`, `/signin`, `/account/login`, or `/account/signin`.

This is required because the field screenshot's normal public Recu homepage visibly showed Sign In/Create Account.

## 3.4 Normal public Recu shell expanded
`_has_normal_recu_shell()` now recognizes both historic and field-observed page markers, including:
- `The Ultimate Chaturbate Archive`
- `The Biggest Chaturbate Archive`
- `Most Bookmarked Recordings`
- existing search/logo markers.

A normal public shell can represent a legitimate zero-result listing even without a signed-in account shell.

## 3.5 No-cookie capture is allowed
If Chrome exports zero Recu-domain cookies, Capture still succeeds as an explicit browser-session confirmation and selects `navigation` as the preferred transport. This avoids another verification loop where the browser itself works but there is no useful copied-cookie transport.

## 3.6 Session schema/version
Captured Recu session schema is now `2143`. Older session transport preferences are migrated conservatively:
- cookie available -> `cookie_http`;
- no cookie -> `navigation`.

`recu_status.session_captured` now uses `captured_at` OR cookie presence instead of requiring a non-empty cookie string.

## 3.7 Diagnostics improved
The `/api/recu/capture` endpoint now logs a safe exception type/message on capture failure. Browser navigation logs the exact challenge marker or actual login redirect when it classifies a session as invalid. Cookie values are never logged.

## 3.8 UI wording
Mobile Settings now says **Capture current session** rather than Capture verified session and explicitly explains that a normal public Recu page can still show Sign In. The user only needs to complete a Cloudflare human check if one is actually shown.

## 3.9 PWA version
Cache/version bumped to `v2143`; server version `CTBRecMobile/2.14.3`.

# 4. IMPORTANT PRESERVED v2.14 FEATURES

Do not regress these while fixing authentication:
- persistent per-model/per-kink scan memory;
- newest-to-oldest scan order;
- stop scanning a kink once a previously seen session ID/date boundary is reached;
- merge only newly discovered moments into cache;
- up to 12 concurrent listing requests on the fast path;
- bounded 429 backoff/shared cooldown;
- persistent Cookie/User-Agent HTTP path as preferred fast path when accepted;
- browser `Page.navigate` + `outerHTML` fallback when HTTP is rejected;
- Originals **and Review** Recu support;
- cache remains usable when refresh fails;
- no Recu refresh/modal is allowed to block mosaic sorting.

# 5. OTHER CURRENT PRODUCT FEATURES THAT MUST REMAIN

All v2.13/v2.12/v2.11 functionality remains part of the product contract, including:
- 4,100+ model progressive mobile rendering and stale-tab response sequencing;
- paired-device auth recovery without false PIN screens;
- READY mosaic counts preserved on model open;
- EZ Sort durable metadata independent of live model-controller availability;
- protected Settings revision writes;
- background mosaic liveness/ghost-queue lease expiration/bounded retries;
- batched FFmpeg extraction for Original, Review and NSFW paths;
- precise Review frame-cut queue, verify-before-delete and undo;
- desktop direct-original Range preview with correct suffix ranges and remux fallback;
- next mosaic scrolls to top only when the chunk actually changes;
- identity-verified live CTBRec bridge with no silent models.json mutation.

# 6. VALIDATION COMPLETED

All Python source compiles and `static/app.js` + `static/rapid.js` pass Node syntax checking.

The complete current validation suite passes, including:
- v2.13 recovery/transport invariants;
- no AbortController catalog regression;
- v2.13 field regressions (settings, READY state, Review Recu path, real FFmpeg mosaic path);
- 4,100-model browser DOM/race tests;
- core catalog/EZ Sort/open-fast tests;
- Original/Review FFmpeg batching;
- HTTP diagnostics;
- NSFW batching;
- v2.14.1 direct-disk preview;
- v2.14.1 Recu auth resilience;
- v2.14.1 scroll reset;
- v2.14 incremental Recu scan memory;
- v2.14.2 compatibility capture regression;
- **new v2.14.3 explicit-capture/challenge regression**.

The new regression specifically proves:
- normal Recu HTML containing `challenge-platform`, `Cloudflare Ray ID`, Sign In and a hidden password field is **not** a challenge/login failure;
- actual `cf-chl-` / `Just a moment` challenge HTML is detected;
- Capture succeeds without `_page_target`, DOM inspection, navigation, or HTTP probe;
- captured session schema is 2143;
- no-cookie Capture falls back to navigation rather than failing;
- a normal public zero-result Recu shell is accepted as valid.

Generic `mobile_self_test.py` cannot fully pass in this Linux build container because `nudenet` is not installed here. Dedicated product regression suites and compile checks are green.

# 7. EXACT FIELD TEST FOR USER

1. Overlay v2.14.3 PATCH onto the current v2.14.2 install.
2. Restart Reviewer.
3. Fully close/reopen iPhone PWA once (cache v2143).
4. Mobile Settings -> Recu -> Start verification browser.
5. On PC/Chrome: if normal Recu content is visible (like the supplied screenshot), do **not** treat the Sign In button as a failure. Complete Cloudflare only if a human-check page is actually shown.
6. Tap **Capture current session**.
7. Expected: HTTP 200 and a message that the current session was captured. Capture must not navigate/reload the Recu tab.
8. Open an Original model and refresh Recu. Then test a Review model.
9. If cookie HTTP receives 403, Reviewer should fall back to Chrome navigation. It must only request verification if that real Chrome navigation shows one of the four proven challenge markers or actually redirects to a login route.
10. If anything still fails, upload a fresh `mobile_reviewer.log` + diagnostics ZIP. Do NOT upload `mobile_recu_session.json` because it contains credential cookies.

# 8. PLANNED / DEFERRED WORK

Only after v2.14.3 is field-confirmed:
- consider optimizing the old CookieJar/raw-HTTP concurrency internals without changing verification semantics;
- optionally add a Recu transport diagnostics card showing `cookie_http` vs `navigation` usage and recent fallback reasons;
- optionally add a user-triggered Full Historical Rescan that ignores incremental stop boundaries;
- do not broaden challenge-token heuristics unless grounded in a real failing page and a regression fixture.

# 9. PACKAGE CONTENT / SAFETY

PATCH must never overwrite runtime/private state: `mobile_config.json`, `recording_roots.txt`, queues, catalog/READY/background states, Recu cache/session/browser profile, Reviewer metadata, or media. Full-source package is the sanitized complete current source.

---

# APPENDED PRIOR HANDOFF (v2.14.2 and earlier)

# CTBRec Mobile Reviewer v2.14.2 — Complete Development Handoff

**Last updated:** 2026-09-11  
**Current baseline:** v2.14.1 + v2.14.2 Recu proven-session capture/fetch hotfix  
**Current status:** **AUTOMATED REGRESSION GREEN; LIVE RECU FIELD CONFIRMATION REQUIRED.**

This v2.14.2 section is authoritative. The complete v2.14.1 and earlier handoff is appended below unchanged for continuity.

## NON-NEGOTIABLE USER DELIVERY RULE

With **EVERY assistant development message, whether final, partial, diagnostic, WIP, or blocked**, provide all three current downloadable artifacts:

1. complete current **FULL SOURCE ZIP**;
2. current safe **PATCH/UPDATE ZIP**;
3. complete current **HANDOFF Markdown** containing implemented + planned work, validation/failures, known issues, architecture/context, safety rules and exact next steps.

If work is unfinished, still produce all three and label them WIP/interim. Never describe newer code while silently linking an older build.

---

# 0. USER FIELD FAILURE THAT CREATED v2.14.2

After v2.14.1, the user reported that pressing **Capture verified session** itself still immediately produced:

> Recu verification/login is required. Complete it in the dedicated Chrome window and capture the session again.

The user emphasized that the visible dedicated Chrome window was already verified and instructed the project to **leverage the previously working Recu methodology** rather than keep inventing new authentication behavior.

That methodology already exists inside the bundled mosaic engine as `fetch_recu_html()` and is the pre-v2.14 path that had worked in the user's installation: persistent Cookie/User-Agent profiles, canonical Recu redirect handling, exact legacy headers, nearby credential-profile recovery, remembered working profiles, cookie-jar persistence, and bounded 403 cooldown/retry behavior.

---

# 1. ROOT-CAUSE ANALYSIS

## 1.1 v2.14.1 still made Capture depend on a fresh automation-driven navigation

`RecuBrowserBridge.capture_session()` in v2.14.1 did successfully read `Storage.getCookies`, but then **navigated the browser again** and refused to persist the capture unless that second automation-driven page load passed its DOM heuristics.

That violates the user's actual workflow: the human-visible Chrome tab is already verified. Capture should capture that browser state, not force a second navigation and use the result as a prerequisite.

## 1.2 DOM login detection could falsely classify a normal authenticated page

The v2.14.1 login heuristic looked for password/login markup. Modern sites can retain dormant login form markup in the DOM even while the user is authenticated. Recu's authenticated shell contains strong positive markers such as `/account/signout`, `Sign Out`, and `My Account`.

v2.14.2 therefore makes a **positive authenticated shell win over dormant login-form markup**. An authenticated page is not marked logged out merely because hidden login UI exists in its DOM.

## 1.3 The reliable old retrieval method was being bypassed while Chrome was running

Even though the bundled mosaic engine retained the proven `fetch_recu_html()` implementation, v2.14.0/2.14.1 routed model scans through newer `RecuBrowserBridge.fetch_html()` behavior whenever the dedicated Chrome process was running. This allowed experimental transport/navigation behavior to take precedence over the method that had previously worked.

v2.14.2 explicitly migrates existing v2.14.x captured sessions back to **`cookie_http`** and restores the old persistent-session implementation as the primary normal retrieval path.

## 1.4 Capture-probe failure was treated too seriously

A post-capture access probe can fail for reasons unrelated to the validity of the browser cookies: Cloudflare transport binding, temporary network behavior, or differences between top-level browser and raw HTTP requests.

v2.14.2 persists the captured cookies **before** probing. Probe failure is logged and returned as a warning, but it never discards the newly captured session and never turns the Capture button into a false verification loop.

---

# 2. v2.14.2 IMPLEMENTED BEHAVIOR

## 2.1 Capture Verified Session no longer navigates Recu

Capture now:

1. attaches to the dedicated Chrome debugging endpoint;
2. reads `Browser.getVersion` and `Storage.getCookies`;
3. extracts Recu cookies + exact browser User-Agent;
4. optionally inspects the **already-open current Recu tab in place** via `Runtime.evaluate` — no `Page.navigate`;
5. rejects only an explicit still-visible challenge/login state when there is no positive authenticated shell;
6. writes `mobile_recu_session.json` immediately;
7. applies the Cookie/User-Agent to the mosaic engine;
8. sets the primary transport to `cookie_http`;
9. performs a non-fatal persistent-session access probe.

If that probe fails, Capture still succeeds and reports that the background probe was deferred. The actual scanner then uses the established recovery path.

## 2.2 Proven pre-v2.14 `fetch_recu_html()` is primary again

At startup the state captures the original bundled `ctbrec_mosaic_sort_lite.fetch_recu_html` function before the mobile bridge monkeypatches the module-level name.

v2.14.2's primary mobile path calls that original implementation through `fetch_html_cookie_http()`.

That implementation provides:

- Recu URL canonicalization, including locale-prefixed Recu hosts;
- exact captured `Cookie` + matching User-Agent profile;
- persistent cookie jar;
- warm-up behavior;
- historical/nearby compatible credential-profile recovery;
- remembered successful profile;
- automatic 401/403 handling;
- bounded cooldown after all known credential profiles are rejected;
- anonymous fallback when enabled;
- normal retry/backoff behavior.

This is intentionally the reliability baseline requested by the user.

## 2.3 Existing v2.14.0/v2.14.1 session files automatically migrate

Old captured session JSON may contain `preferred_transport=cdp` or `navigation`.

When a session without schema version `2142` is loaded, v2.14.2 ignores that old preference and restores:

`preferred_transport = cookie_http`

No user recapture is required solely for this migration.

New captures write:

`schema_version = 2142`

and `preferred_transport = cookie_http`.

## 2.4 Browser navigation remains a last fallback only

If persistent-cookie retrieval fails with an auth-looking result, Reviewer may still confirm through the dedicated Chrome profile. But browser navigation is now **secondary**, not the first path for Capture or normal scanning.

Only an explicit challenge/login state confirmed in that fallback should become a real reauthentication prompt.

## 2.5 Incremental Recu memory is preserved

v2.14.0's requested running memory remains intact:

- per-model known sessions;
- per-kink session/date memory;
- newest-to-oldest traversal;
- stop on the first prior known session/date for that kink;
- merge only new sessions into the durable Recu cache;
- preserve existing markers/timings.

## 2.6 Concurrent scheduling is preserved, with an important performance caveat

The scraper still schedules up to `recu_concurrent_requests` (default 12) listing requests concurrently.

However, the proven legacy `_RecuHttpSession` intentionally holds a per-profile lock around its urllib opener/cookie jar. With one successful credential profile, **actual wire concurrency may be lower than 12** even though the scan scheduler is issuing 12 tasks.

This is an intentional v2.14.2 reliability tradeoff. Do **not** weaken the now-working authentication path merely to restore theoretical concurrency.

Planned after field confirmation: add isolated worker-local persistent sessions (or re-enable CDP strictly as an optional accelerator) while keeping capture/auth state completely separate from acceleration failures.

---

# 3. OTHER CURRENT FEATURES THAT MUST BE PRESERVED

Everything from v2.14.1 remains unless explicitly superseded above, especially:

- v2.14 incremental Recu scan memory;
- Originals + Review Recu support;
- desktop direct-original Range preview and remux fallback;
- browser-correct suffix/open-ended HTTP Range handling;
- next-mosaic scroll reset to the top only on actual chunk changes;
- v2.13 mobile performance fixes and 4,100-model bounded rendering;
- catalog request race protection without AbortController;
- durable EZ Sort membership;
- READY-count preservation / open-fast semantics;
- v2.13 FFmpeg batch mosaic + NSFW extraction;
- v2.12 precise Review frame cuts with verify-before-delete;
- v2.11.3 background liveness/retry/skip protections;
- identity-verified CTBRec live bridge.

---

# 4. VALIDATION STATUS

Automated validations run against the v2.14.2 working source:

- Python syntax compilation: PASS.
- JS syntax (`app.js`, `service-worker.js`): PASS.
- all validation scripts from v2.13 through v2.14.2: PASS.
- new v2.14.2 capture regression: PASS.
  - capture reads cookies;
  - current authenticated shell can contain dormant password markup without false logout;
  - Capture does **not** call navigation;
  - synthetic persistent-HTTP probe failure remains non-fatal;
  - session still persists and applies;
  - old `navigation` session preference migrates to `cookie_http`.
- prior v2.14 incremental Recu suite: PASS.
- prior v2.14.1 direct-disk preview and scroll reset suites: PASS.
- final FULL SOURCE archive extraction + Python/JS syntax: PASS.
- final PATCH overlay preservation against seeded runtime/config/root files: PASS.

Container limitation: the generic `mobile_self_test.py` cannot fully pass in the Linux build environment because `nudenet` is not installed there. This is an environment dependency limitation, not a v2.14.2 application regression. Python/JS and dedicated product regressions are green.

**Live field test still required** because the development environment cannot connect to the user's authenticated Recu Chrome profile.

---

# 5. FIELD TEST CHECKLIST

After applying the patch:

1. stop Reviewer;
2. overlay the v2.14.2 patch;
3. restart Reviewer;
4. fully close/reopen the iPhone PWA once (cache `v2142`);
5. open the dedicated Recu Chrome window and visibly confirm Recu is already normal/verified;
6. press **Capture verified session**;
7. expected: Capture succeeds immediately and **does not navigate the browser away/reload it as a prerequisite**;
8. open an Original or Review model;
9. expected: Recu scanning uses the persistent Cookie/User-Agent path first;
10. if Recu still fails, collect `CTBRec_Mobile_Reviewer_Diagnostics_*.zip` plus `mobile_reviewer.log`. Do **not** upload raw `mobile_recu_session.json` because it contains session credentials.

New log lines are non-secret and should show whether capture stored cookies and whether the current tab looked verified. Cookie values are never logged intentionally.

---

# 6. EXACT NEXT DEVELOPMENT STEPS

Only after v2.14.2 is field-confirmed:

1. measure real Recu scan throughput;
2. if the legacy per-session lock makes the requested 12-way scan materially slower, implement **isolated worker-local HTTP sessions** that use the same proven header/cookie methodology without sharing a mutable CookieJar across threads;
3. retain a global 429 cooldown so 12 workers do not stampede the site;
4. do not make CDP/Network.loadNetworkResource an authentication oracle again;
5. consider exposing a small Recu diagnostics row: primary transport, pages fetched, new sessions, known-boundary stops, most recent rate-limit delay;
6. keep the non-negotiable three-artifact packaging rule on every message.

---

# 7. CURRENT RELEASE ARTIFACTS

Current expected artifact names:

- `CTBRec_Mobile_Reviewer_v2_14_2_FULL_SOURCE.zip`
- `CTBRec_Mobile_Reviewer_v2_14_2_PATCH.zip`
- `CTBRec_Mobile_Reviewer_v2_14_2_HANDOFF.md`
- `TEST_REPORT_v2_14_2.txt`
- `SHA256SUMS_v2_14_2.txt`

---

# HISTORICAL HANDOFF — v2.14.1 AND EARLIER

# CTBRec Mobile Reviewer v2.14.1 — Complete Development Handoff

**Last updated:** 2026-09-12  
**Current baseline:** v2.14.0 incremental Recu/CDP/direct-disk preview + v2.14.1 Recu verification/transport resilience hotfix  
**Current status:** **FINAL / GREEN FOR AUTOMATED REGRESSION GATES; LIVE RECU + DESKTOP PREVIEW FIELD CONFIRMATION REQUIRED.**

This v2.14.1 section is authoritative. Historical v2.14.0 and earlier handoff material is appended below for full continuity.

## NON-NEGOTIABLE USER DELIVERY RULE

With **EVERY assistant development message, whether final, partial, diagnostic, WIP, or blocked**, provide all three current downloadable artifacts:
1. complete current **FULL SOURCE ZIP**;
2. current safe **PATCH/UPDATE ZIP**;
3. complete current **HANDOFF Markdown** containing implemented + planned work, validation/failures, known issues, architecture/context, safety rules and exact next steps.

If work is unfinished, artifacts must still be produced and clearly labeled WIP/interim. Never silently substitute an older version while describing newer code.

---

# 0. USER FIELD FAILURE THAT CREATED v2.14.1

After installing v2.14.0, the user reported:

> Recu opens the browser and captures the cookies, but clicking a model immediately says Recu verification is needed again.

They explicitly asked for a thorough root-cause analysis and a fix. They then added a second field report: **desktop direct-from-disk preview was also not functioning correctly**.

The Recu failure was a regression introduced by the v2.14.0 transport optimization. The previously working v2.13.3 path used verified browser navigation as the reliable browser-backed fetch path. v2.14.0 added experimental CDP `Network.loadNetworkResource` and 12-way concurrent loading but accidentally promoted **transport-specific rejection** into **global authentication failure**.

The desktop preview failure had two independent bugs in v2.14.0: the client only enabled original-file playback when the browser URL itself was literally `localhost`/`127.0.0.1`, so using the recording PC through its own Tailscale hostname silently disabled the feature; and the HTTP Range implementation parsed suffix requests such as `Range: bytes=-65536` incorrectly as bytes `0-65536`. Browser media engines commonly request the tail of MP4/MOV files to locate metadata. A third fallback bug then retried the same direct endpoint for some MP4 files instead of switching to remux.

---

# 1. ROOT-CAUSE ANALYSIS

## 1.1 Primary logic bug: the promised fallback was bypassed for the exact failure class

File: `ctbrec_mobile_server.py`, `RecuBrowserBridge.fetch_html()` / `fetch_many()` in v2.14.0.

v2.14.0 did this:

- try `Network.loadNetworkResource`;
- if `RecuReauthRequired` is raised, immediately re-raise it;
- only non-auth exceptions reached the navigation fallback.

Therefore, if the experimental CDP transport returned a challenge-like page, login-like page, 401/403, bogus 404, or listing-less HTML, v2.14.0 **skipped the fallback entirely** and set the queue to `needs_verification`, even though the visible verified Chrome browser could still be fully authenticated.

That is the most direct explanation for the field symptom: **capture succeeds, then model scan immediately asks to verify again**.

## 1.2 Authentication state was incorrectly conflated with transport state

`Network.loadNetworkResource` can fail or be treated differently from a top-level browser navigation even when it uses `includeCredentials=true`. Chrome documents this CDP method as **experimental**. It must not be used as the authoritative oracle for whether the human-verified browser session itself is valid.

v2.14.1 explicitly separates:

- **authentication/session health** — proven only by the verified Chrome navigation path;
- **transport health** — whether CDP background load or captured-cookie raw HTTP is accepted.

A transport can now be demoted without invalidating authentication.

## 1.3 v2.14.0's empty-listing heuristic was too broad

`_validate_recu_html(... expect_listing=True)` previously treated **any** page without a recognized `video-thumb` / recording link as expired authentication.

That can be false for:

- a legitimate kink page with zero matching recordings;
- a temporarily empty result page;
- a normal signed-in page whose recording DOM differs slightly from the parser expectation.

The user's saved real Recu pages show a strong signed-in shell marker: `/account/signout`, `top-signin-signout-button`, `Sign Out`, `My Account`. v2.14.1 uses these markers to distinguish a legitimate authenticated zero-result page from an unverified/redirect/challenge response.

## 1.4 Capture success was too weak

v2.14.0 `capture_session()` considered `Storage.getCookies` returning Recu cookies sufficient to report capture success. That proves only that cookie records exist in Chrome. It did **not** prove that the browser could currently load a normal Recu page.

v2.14.1 capture now:

1. reads the Recu cookies + user agent;
2. performs a real navigation through the exact verified Chrome profile;
3. refuses to persist a *verified* session if that browser navigation still shows challenge/login/failure;
4. only after browser validation probes fast transports;
5. chooses the fastest transport that actually works, while retaining verified navigation as the reliability fallback.

## 1.5 v2.14.0 validation missed this exact interaction

The v2.14.0 synthetic suite separately verified:

- challenge/login classification;
- incremental memory;
- CDP method presence;
- 12-way fetch wiring.

But it did **not** test the crucial compound case:

> CDP says "auth problem" while real verified Chrome navigation succeeds.

v2.14.1 adds that exact regression case for both single fetch and 12-way batch fetch.

## 1.6 Closed-browser captured-cookie rejection could still create a false verification loop

Even with the browser-backed path fixed, there was another edge case: if the user captured a session and later the dedicated Chrome window was closed, the scraper fell back to copied-cookie HTTP. Recu/Cloudflare can reject that copied-cookie transport while the persisted browser profile is still valid. The old path could interpret that as expired authentication.

v2.14.1 now treats raw/captured-cookie 401/403/bogus-404/challenge/login-like failures as **transport-suspicious, not auth-authoritative**. It automatically launches/attaches the exact dedicated Recu Chrome profile and confirms the requested page with real navigation before asking the user to authenticate again.

## 1.7 Desktop original-file Range implementation was not browser-correct

The direct-disk preview endpoint supported simple `bytes=start-end` ranges but mishandled RFC suffix ranges. For example, a browser request for the final 64 KiB:

`Range: bytes=-65536`

was incorrectly served as bytes `0-65536`. MP4/MOV metadata can live near the end of a file, so this can make a perfectly valid source appear unplayable. The endpoint now handles open-ended and suffix ranges correctly and returns HTTP 416 with `Content-Range: bytes */SIZE` for unsatisfiable ranges.

## 1.8 Desktop detection was tied to the URL hostname rather than the device type

`isDesktopPreviewClient()` required both a desktop user-agent and a literal hostname of `localhost`, `127.0.0.1` or `::1`. If the user sat at the recording PC but opened Reviewer through its Tailscale hostname, the direct-original path never ran. Browsers do not expose a reliable "this page is physically on the same Windows machine" primitive. v2.14.1 therefore lets any desktop browser try the original-file Range path first. On the recording PC this is disk -> loopback/Tailscale proxy -> browser with zero ffmpeg; on another desktop it is still a zero-transcode original stream. Mobile behavior remains unchanged.

## 1.9 MP4 direct-playback fallback could retry the exact same failing path

For MP4/M4V/MOV sources, `item.url` was itself a direct Range URL. The v2.14.0 error handler used `item.url` as the "fast remux" fallback, so a direct-decode failure could simply retry direct-decode again. v2.14.1 adds an explicit `remux_url` and uses that first; Compatibility transcode remains the last-resort manual fallback.

---

# 2. v2.14.1 IMPLEMENTED FIXES

## 2.1 Capture validates the actual browser session

`RecuBrowserBridge.capture_session()` now performs verified Chrome navigation before declaring success.

Persisted session metadata now includes:

- `validated_at`
- `validation_transport = "verified_navigation"`
- `authenticated_shell`
- `preferred_transport`
- `transport_reason`

Cookie values remain runtime-private as before.

## 2.2 Adaptive transport selection

The bridge now maintains a runtime preferred transport:

- `cdp`
- `cookie_http`
- `navigation`
- `auto` before probing

After capture:

1. reliable browser navigation must succeed first;
2. `Network.loadNetworkResource` is probed;
3. if CDP is unsuitable, the captured-cookie raw HTTP client is probed;
4. if both fast transports are unsuitable, the session remains **valid** and the bridge uses verified navigation.

This restores the important behavior from the separate Recu clip scraper: 12-way raw HTTP can be used when accepted, while reliable browser navigation is the fallback.

## 2.3 Fast transport failures no longer cause false re-auth

New helpers:

- `_navigation_fallback_one(...)`
- `_navigation_fallback_many(...)`

If CDP or captured-cookie HTTP returns an auth-looking failure, v2.14.1 checks the same URL through verified Chrome navigation.

- If navigation succeeds: auth remains valid; fast transport is demoted.
- If navigation also shows challenge/login: `RecuReauthRequired` is surfaced.

This is the central behavioral invariant:

> **Only authoritative verified-browser failure can ask the user to re-authenticate while that browser is running.**

## 2.4 Legitimate authenticated empty pages are accepted

Added:

- `_has_authenticated_shell(html)`
- `_has_normal_recu_shell(html)`

A signed-in page containing `/account/signout` / Sign Out / My Account can return zero clip tiles without being mislabeled as an expired session.

An unknown listing-less page still remains auth-suspicious and is confirmed through verified navigation.

## 2.5 Fast captured-cookie HTTP path is first-class again

Added:

- `fetch_html_cookie_http(...)`
- `fetch_many_cookie_http(...)`

These reuse the pre-v2.14 proven Recu HTTP client with the exact Cookie/User-Agent captured from Chrome.

Batch size remains up to 12 by default.

## 2.6 Settings surface now shows useful state

`recu_status()` and `static/app.js` now expose/render:

- session captured timestamp;
- browser verified timestamp;
- active transport (`fast CDP`, `fast captured-cookie HTTP`, or `verified navigation fallback`).

This makes future diagnosis much easier and prevents the UI from pretending a cookie capture is equivalent to verified access.

## 2.7 Recu clear resets transport health

Clearing the Recu session resets transport mode to `auto` so a future capture gets a clean capability probe.

## 2.8 Stale Review API text corrected

The Recu refresh error message now correctly says Recu is available for **Originals/Review** queues, not Originals only.

## 2.9 PWA shell bumped

Current cache: `ctbrec-shell-v2141`.

`index.html` asset query versions are `2141`.

## 2.10 Closed-browser Recu auth confirmation

`MobileReviewerState._recu_fetch_bridge()` now catches auth-looking copied-cookie/raw-HTTP rejection when Chrome is not running and calls `_ensure_recu_browser_navigation(...)`. That function starts/attaches the exact persisted Recu Chrome profile and makes real navigation authoritative before surfacing re-auth.

The post-scan auth heuristic also performs browser confirmation rather than blindly converting raw HTTP error text into `needs_verification`.

## 2.11 Browser-correct direct original-file preview

Added `_parse_http_byte_range(...)` and corrected `send_range_file(...)` so desktop media requests support:

- normal ranges (`bytes=100-199`);
- open-ended ranges (`bytes=100-`);
- suffix ranges (`bytes=-65536`);
- HTTP 416 for unsatisfiable ranges;
- `Content-Disposition: inline`.

Preview payload now exposes an explicit `remux_url`. Desktop browsers try `local_disk_url` first regardless of whether the dashboard was opened through localhost or a Tailscale hostname. If native decode fails, the client falls back to `remux_url`, not another direct Range URL.

---

## 2.12 Mosaic advance scroll reset

User additionally requested that after sorting one mosaic, the next mosaic always opens from its top rather than preserving the previous chunk's deep scroll position.

Implementation in `static/app.js`:
- `loadCurrent()` compares the previous and next chunk signatures;
- only when the signature changes does it call `scrollReviewToTop()` after rendering;
- the helper performs a post-layout double-`requestAnimationFrame` reset to the review view/page top;
- same-chunk background status/Recu/model-control refreshes intentionally do **not** move the viewport.

This applies naturally to Submit/Skip/Back and any other navigation path that loads a different queue chunk.

Validation: `validation/validate_v2_14_1_scroll_reset.py`.

# 3. BEHAVIOR THAT MUST NOT REGRESS

All v2.14.0 behavior remains required:

- persistent per-model/per-kink Recu session memory;
- incremental newest-to-oldest scan;
- stop at prior session/date boundary;
- existing cached moments retained/merged;
- up to 12 concurrent fast listing fetches;
- shared 429 backoff;
- lightweight verified navigation fallback blocks heavy image/video/font assets and uses `Page.stopLoading` as soon as needed DOM exists;
- Recu works in Original and Review queues;
- desktop original-file HTTP Range preview with browser-correct Range semantics and explicit remux/compatibility fallback;
- no Recu modal may block sorting.

All v2.13.3 safety/performance behavior also remains required:

- true READY count preserved on model open/back;
- exact-signature ready snapshot hydration;
- EZ Sort durable sidecar behavior;
- Settings revision guard and corruption repair;
- no model-list AbortController regression;
- bounded mobile DOM rendering;
- batched FFmpeg extraction;
- background worker retry/skip/liveness protections;
- precise Review frame cuts are durable/idle-only/verify-before-original-deletion;
- identity-verified CTBRec live bridge; never silently mutate `models.json`.

---

# 4. VALIDATION COMPLETED FOR v2.14.1

## Syntax / static

- `ctbrec_mobile_server.py`: Python compile PASS
- `ctbrec_mosaic_sort_lite.py`: Python compile PASS
- `static/app.js`: Node syntax PASS
- `static/rapid.js`: Node syntax PASS

## Existing regression suites

PASS:

- `validate_v2_13_1_recovery.py`
- `validate_v2_13_2_catalog_abort.py`
- `validate_v2_13_3_field_regressions.py`
- `validate_v2_13_browser_dom.py`
- `validate_v2_13_core.py`
- `validate_v2_13_ffmpeg_batch.py`
- `validate_v2_13_http.py`
- `validate_v2_13_nsfw_batch.py`
- `validate_v2_14_recu_incremental.py`

## New v2.14.1 regression suite

`validation/validate_v2_14_1_recu_auth_resilience.py` verifies:

1. authenticated signed-in empty listing is accepted;
2. unknown empty listing remains auth-suspicious;
3. CDP raises synthetic `RecuReauthRequired` + captured HTTP fails + navigation succeeds => **NO reauth**, transport demoted to navigation;
4. same behavior for batch fetch;
5. if authoritative navigation also raises `RecuReauthRequired`, then reauth correctly propagates;
6. if Chrome is closed and copied-cookie HTTP returns synthetic HTTP 403, the state-level bridge confirms through the persisted browser-navigation fallback instead of requesting verification.

PASS.

## New v2.14.1 desktop preview regression suite

`validation/validate_v2_14_1_preview_disk.py` verifies:

1. standard, open-ended and suffix byte ranges;
2. unsatisfiable ranges are rejected;
3. desktop original-file mode is not disabled merely because the same PC uses a Tailscale hostname;
4. direct decode failure falls back to explicit `remux_url` rather than retrying the same direct path.

PASS.

## Environment caveat

The Linux build container does not have the `nudenet` Python package installed, so `mobile_self_test.py` exits at its NudeNet dependency check. This is not a v2.14.1 code regression; the user's Windows installation already showed NudeNet installed. The complete targeted regression suite and syntax gates pass in the build tree.

---

# 5. EXACT USER FIELD TEST FOR v2.14.1

After applying the patch:

1. stop/restart Reviewer;
2. fully kill/reopen the iPhone PWA once (v2141 cache);
3. Mobile Settings -> Recu -> Launch browser;
4. complete Recu/Cloudflare verification normally;
5. tap **Capture verified session**;
6. expected message now explicitly says the browser session was **verified**, not merely that cookies were captured;
7. Settings status should show one of:
   - `transport: fast CDP`
   - `transport: fast captured-cookie HTTP`
   - `transport: verified navigation fallback`
8. open an Original or Review model;
9. Recu should scan without immediately reverting to `verification needed`.

If CDP is what was breaking the session in the field, the likely result is that v2.14.1 automatically chooses captured-cookie HTTP or navigation and simply keeps working.

If the browser itself genuinely returns Cloudflare/login during the authoritative navigation check, capture will now fail immediately and correctly ask for verification instead of falsely reporting success.

Desktop preview field check:

1. open Reviewer on the Windows recording PC, either via localhost **or that PC's Tailscale hostname**;
2. preview an MP4/MOV source and confirm the note says direct original-file playback and no ffmpeg preview process starts;
3. preview a TS/MKV/unsupported-codec source and confirm it automatically falls back to fast remux instead of remaining blank;
4. scrub/seek in a large MP4 to exercise suffix/open-ended Range requests.

---

# 6. IF FIELD FAILURE STILL OCCURS

Do **not** redesign auth again blindly.

Collect/upload:

- `mobile_reviewer.log`
- the current diagnostics ZIP from `collect_mobile_diagnostics.bat`
- sanitized `mobile_recu_session.json` with the `cookie` value removed (cookie names/timestamps/transport fields are useful)

Look in `mobile_reviewer.log` for new v2.14.1 messages:

- `Recu CDP transport demoted...`
- `Recu authentication remained valid...`
- `Recu CDP and captured-cookie HTTP both failed...`
- actual authoritative navigation error

Those messages now distinguish transport rejection from real authentication loss.

---

# 7. PLANNED / OPTIONAL FOLLOW-UP WORK

Not required for the v2.14.1 hotfix, but valid future improvements:

1. Add a one-tap **Recu transport diagnostic** button showing one probe result for CDP / captured HTTP / navigation separately.
2. Add per-transport counters/timing to diagnostics so performance can be measured on the user's actual PC.
3. Add optional periodic fast-transport re-probe after navigation fallback has been stable for a long period; do not re-probe on every model.
4. Add a manual **Full Historical Recu Rescan** action. Normal refresh must remain incremental and stop at prior boundaries.
5. Consider multiple independent CDP targets for concurrent navigation only if proven safe; current reliability fallback remains sequential by design.

---

# 8. PACKAGE / UPGRADE RULES

PATCH from v2.14.0 must include only changed program/docs/validation files and must **not** overwrite runtime/private state such as:

- `mobile_config.json`
- `recording_roots.txt`
- `mobile_action_queue.json`
- `mobile_catalog_cache.json`
- `mobile_ready_work_index.json`
- Recu cache/session/browser profile
- mosaics/media
- NudeNet model/runtime state

FULL SOURCE is the complete current sanitized source tree and must not leak active session cookie values or browser-profile contents.

Every future assistant message must still provide FULL SOURCE ZIP + PATCH ZIP + current HANDOFF Markdown.

---

# 9. EXACT NEXT STEP FOR ANOTHER AI AGENT

If the user reports success: preserve v2.14.1 as the new Recu baseline and continue only from new requested functionality.

If the user reports another Recu verification loop:

1. inspect the new transport-specific log lines first;
2. identify whether capture says `browser verified` and which transport was selected;
3. determine whether **verified navigation** itself failed or only CDP/HTTP failed;
4. do not mark authentication bad based on CDP/HTTP alone;
5. preserve incremental memory and all sorting UX invariants while fixing the narrow cause.

---

# HISTORICAL v2.14.0 HANDOFF (preserved verbatim below)

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

## v2.14.3 final packaging gate
- PATCH runtime-state preservation overlay: PASS.
- Final FULL SOURCE clean extraction: PASS.
- Extracted archive compile + JS syntax: PASS.
- Extracted archive v2.14.3 capture, v2.14.2 compatibility, v2.14 incremental and v2.14.1 auth-resilience suites: PASS.
