## v2.15.10 — Git Bridge Windows compatibility hotfix

v2.15.10 fixes the first real Windows field failure in `Setup-Git-Bridge.ps1`. PowerShell does not use backslash as an escape character, so the bridge's `TrimStart('\\','/')` call supplied a two-character string to a .NET `System.Char` overload and failed before source copy. Setup and update/deploy path handling now use regex-based separator stripping and normalized path comparison instead. The same audit also fixed a latent Python resolver issue that could reduce `py` to the single character `p`, adds automatic repository-local Git identity from the authenticated `gh` account when needed, and tightens public-source filtering for `.git`/local bridge state. Reviewer feature behavior is unchanged from v2.15.9.

## v2.15.9 — seekable zero-reencode preview cache + fixed preview geometry

v2.15.9 is a preview-performance stabilization release driven by two same-model field diagnostic exports. A smooth MP4/H.264/AAC file used native browser HTTP Range reads and scrubbed in roughly tens to low hundreds of milliseconds. A true MPEG-TS/H.264/AAC file was rejected by Chromium (`DEMUXER_ERROR_COULD_NOT_OPEN`), the old live stream-copy remux collapsed after about 0.19 seconds, and Compatibility required a new FFmpeg transcode startup on every seek. Reviewer now materializes a temporary **seekable MP4 wrapper with `-c copy`** for non-native containers: no video/audio re-encoding, source identity is keyed by path+size+mtime, outputs are atomically written and duration-validated against truncation, then normal Range playback is used and the wrapper is reused until cache expiry/source change. The cache is bounded by age and size and never alters source recordings. The automatic ladder is now native direct/HLS → seekable cached wrapper → Compatibility.

The preview modal is also geometry-stable across method changes: the player viewport has a fixed responsive height, video uses `object-fit: contain`, status text reserves fixed space, and Compatibility controls remain in-layout rather than appearing/disappearing. Switching Direct/seekable/Compatibility can change the source under the player but no longer moves the scrubber or Prev/Next controls while the user is aiming. Preview diagnostics now record seekable-cache build/hit/failure events. All v2.15.8 Git Bridge behavior and unrelated sorting/mosaic/READY/Recu/Keep Last behavior are preserved.

## v2.15.8 — Git Bridge / one-click validated GitHub deployment

v2.15.8 adds the supported CTBRec Git Bridge under `tools/git_bridge/`. The bridge preserves the normal ChatGPT workflow while automating repository/deployment mechanics: bootstrap the public umbrella repository safely, reject private/runtime files, validate each ChatGPT PATCH, create a versioned branch and pull request with release notes/test results, merge/tag the validated release, stop the live Reviewer, back up touched files, apply the patch, run the live self-test, restart the server, verify local health, and roll back the live files if deployment/startup fails. The bundled Codex installer uses `npm.cmd`, avoiding the Windows PowerShell `npm.ps1` execution-policy failure. No Reviewer sorting/mosaic/playback behavior changes in this release.

## v2.15.7 — stabilization: clean chunk state + exact preview duration

v2.15.7 is a stabilization release after the rapid v2.15.x feature run. The Review/Frame Cut fast path now fully isolates per-chunk state: frame/file selections, Shift anchors, restore/delete sets and frame caches are reconstructed only from the current chunk draft, so Queue precise cuts & next cannot carry selections into the next mosaic. Draft autosaves are now bound to the exact chunk signature and stale delayed writes are rejected server-side. Shift-click is symmetric for bulk correction, and Review now has an explicit Clear selections control. Preview duration is resolved from the actual media using ffprobe/ffmpeg (or native direct metadata), not `_tail_...` filename semantics; validated mosaic sidecars also retain the exact duration stored when the mosaic was generated. Rapid Sort was audited against the normal path and now also preserves scroll reset, frame-cache reset, and current bounded look-ahead messaging.

## v2.15.6 — source-timeline preview + automatic playback ladder

v2.15.6 keeps the v2.15.5 direct-playback reliability/diagnostics work but replaces the awkward fallback scrubber UX. Direct/HLS playback still uses native seekable browser controls. When Reviewer must fall back to live FFmpeg remux or compatibility transcoding, the browser's internal pipe timeline is hidden and a source-timeline control is overlaid directly on the video from 0:00 through the full recording duration. Scrubbing that timeline keeps the user's position in source time while Reviewer invisibly restarts the backend at the requested timestamp. Playback method selection is now fully automatic: direct/seekable → fast zero-reencode remux → compatibility transcode. A premature stream end is treated as a failed method for the same file, not as completion of that recording, so multi-file preview no longer races through selected files. Reviewer advances to the next selected file only after the source playhead is genuinely near the end; if every method fails it stays on the current file and preserves Preview diagnostics export. Queue/mosaic/READY/look-ahead/TS-sort/Shift-click behavior is unchanged.

## v2.15.5 — resilient direct preview / fast fallback seeking / TS-only sort / Shift-click repair

v2.15.5 addresses field reports where some MP4 and TS recordings failed direct playback and compatibility streams became painful to scrub. Direct media now uses explicit container MIME types (including `.ts` → `video/mp2t`) rather than OS registry guesses; the startup watchdog no longer stops at metadata-only success; direct seek stalls automatically fall through at the requested source timestamp; and remux/compatibility fallbacks gain server-side Fast seek so a timeline jump restarts FFmpeg at that source position instead of attempting to seek inside a non-seekable pipe. Preview now has a one-click diagnostics ZIP containing client events, server Range request/timing traces, ffprobe metadata and a bounded log tail but no media bytes. Model ordering adds `TS-only size ↓`, populated during the ordinary catalog scan; users upgrading from an older cached catalog should tap Rescan once. Shift-click range selection is hardened by tracking Shift at keyboard and pointerdown as well as click, with a real Chromium regression. Queue/mosaic/READY/look-ahead behavior is unchanged from v2.15.4.

## v2.15.4 — visible-queue lease renewal / false "model no longer open" recovery

v2.15.4 fixes a phone-visibility regression where iOS could delay the 12-second JavaScript heartbeat even while the review screen was visibly open. The server previously treated that timer lease as the sole authority and could cancel current-mosaic or look-ahead generation with “this model is no longer open on the phone” despite the phone continuing to poll that exact queue every ~2.2 seconds. Queue-specific `/current` and `/status` requests now renew the active lease for the queue the phone explicitly opened. Explicit Models/switch-model actions remain authoritative via a durable per-queue `phone_open` flag, so stale requests cannot resurrect work after the user actually leaves. If the false expiry already cancelled current or look-ahead work, a legitimate poll self-recovers it. Restart Reviewer and fully close/reopen the PWA once so cache `v2154` is active.

## v2.15.3 — pending model-open self-heal

v2.15.3 fixes a v2.15.2 race that could leave an Original/Review model permanently stuck on the pending "Opening this model and locating its first chunk…" screen. Pending queues now mirror the real indexing task progress, shared-task cleanup preserves late-joining queue targets, and an active queue whose indexing task finished/cancelled without attaching discovered chunks automatically re-arms indexing. A legitimately running task is not duplicated; zero-sortable-chunk models report a real error; recovery is bounded rather than looping forever. v2.15.2 automatic bounded look-ahead and all prior behavior remain unchanged. Restart Reviewer and fully close/reopen the PWA once so cache `v2153` is active.

## v2.15.2 — bounded automatic active-model look-ahead

v2.15.2 restores the intended automatic open-model look-ahead without restoring the old runaway/hidden-queue behavior. While an Original or Review model remains open, the configured `background_mosaics.<mode>.upcoming_count` is interpreted as the number of **future** mosaics to keep ready in addition to the current mosaic. The buffer is refilled after model indexing, Submit, Skip and Back, and the maintenance loop retries a refill after temporary priority/idle pauses. Leaving or switching models cancels that queue's look-ahead work. A ready subset can no longer be exhausted into a false Done screen while the model's full chunk inventory is still indexing; the model remains open in a preparing state until that inventory attaches. v2.15.1 Shift-click, v2.15.0 model sorting, v2.14.9 reindex scope, v2.14.8 playback/open fixes, and all earlier safety behavior are preserved. Restart Reviewer and fully close/reopen the PWA once so cache `v2152` is active.

## v2.15.1 — desktop Shift-click range selection

v2.15.1 keeps v2.15.0 behavior unchanged except for desktop range selection in the sorter. A normal click still uses the existing additive/toggle behavior. Shift-click selects/applies the current destination to every file/segment between the immediately previous click and the new click, inclusive. Review Frame Cut supports the same range behavior across sampled frames. The range anchor resets on every chunk/model change. No queue, mosaic generation, playback, READY/index, Recu, catalog, or background-worker behavior changed. After overlaying the v2.15.1 patch, restart Reviewer and fully close/reopen the PWA once so cache `v2151` is active.

# CTBRec Mobile Reviewer v2.15.0

Current release: **v2.15.0 — Random Size-Bounded + Average-Chunk Model Sorting**.

## What v2.15.0 adds
The Models screen now has three ordering modes: **Largest total** (the existing behavior), **Random**, and **Avg chunk size ↓**. Random mode can be bounded by minimum/maximum total model size in GB and remains stable until **Reshuffle** is tapped. Average-chunk mode reads already-persisted chunk definitions only; it never launches FFmpeg, scans model folders, generates mosaics, or changes queues. Models without trustworthy current chunk metadata follow the known models by total size until normal indexing reaches them.

## Safety / upgrade
v2.14.9 remains the behavioral baseline for sorting, mosaic reuse/reindexing, playback, queues, and background work. v2.15.0 changes only model-list presentation plus read-only catalog metadata needed for average-chunk ordering. Overlay `CTBRec_Mobile_Reviewer_v2_15_0_PATCH.zip`, restart Reviewer, and fully close/reopen the iPhone PWA once so cache `v2150` is active.

---

# CTBRec Mobile Reviewer v2.14.9

Current release: **v2.14.9 — Scoped Existing-Mosaic Recovery / Truthful Reindex Status**.

## Why v2.14.9 exists
v2.14.8 could run sidecar recovery every time a model was clicked and report that mosaics were reindexed even when zero new mosaics were recovered. v2.14.9 only invokes that repair path when the on-disk sidecar inventory can add valid work beyond the READY queue already loaded. Existing mosaics are reused by source/layout compatibility, not by software release number.

## v2.14.9 upgrade
Overlay `CTBRec_Mobile_Reviewer_v2_14_9_PATCH.zip` onto v2.14.8, restart Reviewer, and fully close/reopen the iPhone PWA once so cache `v2149` is active. The patch excludes runtime/private state.

---

# CTBRec Mobile Reviewer v2.14.8

Current release: **v2.14.8 — Exact-Model On-Demand Sorting / Queue Cancellation / Video Playback Recovery**.

## Why v2.14.8 exists
The field workflow had four linked failures: tapping a model could time out after about 12 seconds; a model with no immediately ready mosaic could be bypassed in favor of another ready model; the clicked model's queue kept accumulating future mosaic work even after the user left it; and video preview frequently failed or stalled from both local disk and phone streaming.

The 12-second failure had a concrete client cause: `/api/queues/open-fast` was called with a hard `timeoutMs: 12000`, while the server could still be reconciling/indexing model metadata. The queue growth had a separate server cause: the clicked-model priority worker walked the model's full chunk list and generated every missing mosaic, while several navigation/maintenance paths also scheduled future prefetch automatically.

## What v2.14.8 changes
- **No automatic model hopping.** Tapping a model opens that exact model. If no mosaic is ready, Reviewer creates a pending queue immediately and stays in that model while its first sortable mosaic is prepared.
- **No 12-second model-open abort.** The `open-fast` call has no client-side deadline; the phone gets a queue/view immediately even when the first mosaic still needs work.
- **Sidecar recovery stays intact without blocking the tap.** v2.14.7 sidecar reconciliation still runs, but for the clicked-model path it runs in the priority worker instead of synchronously before the phone can enter the model.
- **Current-only automatic generation.** Opening/indexing a model attaches a stable chunk inventory but automatically generates only the mosaic currently being viewed. Advancing to the next chunk generates that next mosaic when it becomes current.
- **Automatic future prefetch removed.** Submit, Skip, Back, queue load, and maintenance no longer call `schedule_prefetch()`. **Generate Ahead** remains an explicit manual action only.
- **Leaving/switching models cancels hidden work.** The sorting heartbeat deactivates the old queue, cancels model-specific priority work and Generate Ahead demand, and interactive mosaic generation checks that the queue is still the active phone model before continuing to another checkpoint.
- **No automatic next-ready action remains.** The manual Next Ready Model buttons remain available, but code no longer calls that action automatically; Ignore now returns to Models instead.
- **Video preview now has a full fallback ladder.** Direct/local-disk or native HLS failure/stall automatically retries fast remux; remux failure/stall automatically retries compatibility transcoding.
- **Video stalls are detected.** Direct playback, HLS, remux, and compatibility modes now have bounded startup timers so a source that hangs without firing a browser error is retried automatically.
- **FFmpeg preview paths are more tolerant.** HLS/remux paths use generated timestamps, discard corrupt packets where possible, ignore recoverable decode errors, normalize negative timestamps, and fragmented MP4 output uses `default_base_moof`. HLS segment creation has a bounded subprocess timeout.
- PWA shell cache is **v2148** and server version is `CTBRecMobile/2.14.8`.

## Upgrade
Stop Reviewer, overlay `CTBRec_Mobile_Reviewer_v2_14_8_PATCH.zip` onto the current v2.14.7 installation, restart Reviewer, then fully close/reopen the iPhone Home Screen PWA once so cache `v2148` is active. The patch intentionally excludes runtime/private state.

---
# CTBRec Mobile Reviewer v2.14.7

Current release: **v2.14.7 — Existing Mosaic Reindex / Sort-First Queue Repair**.

## Why v2.14.7 exists
A field model could correctly show ~22 GB in the catalog and physically contain many usable Original mosaics, while Mobile Reviewer exposed only one mosaic and the manual **Ahead / Generate now** action claimed there were no upcoming mosaics.

The root cause was a cache/source-of-truth mistake: phone quick-open trusted `mobile_ready_work_index.json` even when that derived ready-index contained fewer rows than the durable `._chunk_mosaics/*.sources.json` sidecars already present on the recording drive. The manual Ahead task then examined only the queue created from that stale snapshot, so a one-row queue naturally produced `0/0 upcoming` even though many sidecar-backed mosaics existed on disk.

## What v2.14.7 changes
- On Original/Review model open, Reviewer cheaply counts durable mosaic sidecars for that model. If disk has more sidecars than the cached ready queue (or the cache has no usable mosaics), it performs a **sidecar-only reindex** before opening the sorter.
- This recovery reads JSON metadata and file stats only. It does **not** run ffprobe, ffmpeg, frame extraction, Recu, or a full model regeneration.
- Recovered sidecar-backed mosaics are immediately merged into the phone queue and persisted back into the ready index.
- Original sidecar recovery now respects `._cb_chunk_reviewer_state.json`; chunks explicitly marked `done`/`skipped` are never resurrected merely because an old sidecar remains.
- **Ahead / Generate now** now performs the same sidecar reconciliation before deciding how many upcoming mosaics exist. A stale one-row queue can no longer incorrectly report `0/0` while valid on-disk mosaics exist.
- The phone toast reports when existing mosaics were recovered from disk metadata.
- Orphan legacy JPEGs that have no trustworthy sidecar remain ignored rather than being treated as sortable work. The v2.14.4 **Rebuild chunk + mosaic** action remains the safe way to replace/remove stale chunk artifacts.
- PWA shell cache is **v2147** and server version is `CTBRecMobile/2.14.7`.

## User-data diagnosis that drove the fix
The supplied `._chunk_mosaics` bundle contained 25 JPEGs but only 16 `.sources.json` sidecars. Those 16 sidecars referenced 103 source recordings totaling about **22.45 GiB**, matching the model's ~22 GB catalog size. Nine extra JPEGs were old orphan duplicates with shifted chunk numbers and no sidecars. The supplied review-state file marked only one separate March 12 chunk as `done`. Therefore the model had many durable sortable mosaics on disk; exposing only one was a stale ready-index/queue problem, not a missing-media problem.

## Upgrade
Stop Reviewer, overlay `CTBRec_Mobile_Reviewer_v2_14_7_PATCH.zip` onto the existing v2.14.6 installation, restart it, then fully close/reopen the iPhone PWA once so cache `v2147` is active. Runtime/private state is intentionally excluded from the patch.

---
# CTBRec Mobile Reviewer v2.14.6

Current release: **v2.14.6 — Per-Session Keep Last Across Drives**.

## What v2.14.6 changes
- Keeps the v2.14.5 tagged-filename parser: CTBRec timestamps can appear before/after `_tail_...`, `_endminus_...`, and segment tags.
- Reverts the v2.14.5 model-wide Keep Last selection scope at the user's explicit request.
- All roots/drives are combined for a model first; drive changes do not create new sessions.
- A new session begins only when the uncovered gap between the previous known recording coverage and the next recording start is **greater than** `gap_minutes` (default 30).
- The model's configured Keep Last duration is applied independently to the tail of **every** detected session.
- Whole-file retention semantics remain unchanged: boundary files are not silently trimmed.
- PWA shell cache is **v2146** and server version is `CTBRecMobile/2.14.6`.

## What v2.14.4 fixes

- Whole-library background mosaic traversal now persists a **durable ordered pass plan** in `mobile_library_mosaic_plan.json`. Mutable model byte totals / drive-size metadata can no longer invalidate the in-progress cursor and restart a multi-thousand-model pass from model 1. New sizes/models are picked up on the next completed pass.
- Removed arbitrary **maximum values** from operational Settings fields and their backend clamps (idle/rescan intervals, prefetch counts, mosaic spacing/columns/width/frame count, NSFW operational values, offline pack sizes, etc.). Semantic ranges that are physically/protocol bounded (ports, confidence probabilities, JPEG quality) remain bounded.
- Model-list tiles now invalidate/reload their cached size data whenever the PC catalog scan timestamp changes, without resetting the current scroll window.
- Original mosaic generation removes stale same-base part JPEGs before writing a fresh canonical set. Mobile layout sidecars are now v3 and include exact frame index/local timestamp and per-source duration.
- Hit maps are validated against the actual JPEG dimensions, source identities, expected frame plan/order/timestamps, and duplicate rectangles. Invalid layouts are rejected/regenerated instead of exposing mismatched tap targets.
- Quick-open READY snapshots de-duplicate exact signature/source-set duplicates.
- Added **Rebuild chunk + mosaic** beside Redo. It re-scans the model folders from disk, verifies which expected files still exist, adds newly discovered files that belong to the current chunk, purges stale/duplicate mosaics tied to the old/current source sets, removes duplicate queued chunks, regenerates one authoritative mosaic/hit map, and persists the reconciled READY snapshot.
- Newly retained media now carries end-relative position metadata such as `_endminus_15m00s_to_0m00s`. Precise Review clips always receive it; whole Original KEEP moves into Review receive it; Review moves into Cumshots/Misc Hot Scenes receive it. This makes tail position visible in filenames for future Keep Last decisions.
- PWA shell cache bumped to **v2144**; server reports `CTBRecMobile/2.14.4`.

## Upgrade

Use `UPDATE_FROM_2_14_3.txt`. Stop Reviewer, overlay the PATCH, restart it, then fully close/reopen the iPhone PWA once. Runtime state is intentionally excluded from the patch.

## Internal engine versions

The embedded Original/Review engine version labels are independent component versions and may remain lower than the Mobile Reviewer package release number.

---

# Prior README (v2.14.3 and earlier)

# CTBRec Mobile Reviewer v2.14.3

Current release: **v2.14.3 — Recu Explicit-Confirmation Verification Fix**.

## v2.14.3 Recu correction

- **Capture current session is now explicit-user-confirmation driven.** If the dedicated Chrome window visibly shows the normal Recu site, clicking Capture accepts that state and captures `Browser.getVersion` + `Storage.getCookies`; Capture no longer inspects the DOM, navigates the tab, or runs an HTTP probe that can reject the user's visible verification.
- Recu public pages are allowed to show **Sign In** controls. Sign In/password markup is no longer interpreted as failed Cloudflare verification. Only an actual redirect to a dedicated login route is treated as a login redirect.
- Cloudflare challenge detection now uses the exact four markers from the working `recu_clip_model_counter_v2_4_1_MOBILE_REVIEWER_SHARED_PROFILE_FULL` implementation: `cf-chl-`, `verify you are human`, `just a moment`, and `checking your browser`. Generic strings such as `challenge-platform` and `Cloudflare Ray ID` were removed because they can appear on ordinary accessible Recu pages.
- Normal public Recu shell markers now include the field-observed **The Biggest Chaturbate Archive** / **Most Bookmarked Recordings** page. A valid public shell is sufficient for an empty listing; account sign-in is not required merely to prove Cloudflare access.
- If Chrome exports Recu cookies, the persistent Cookie/User-Agent HTTP path remains the preferred fast path. If it exports none, Capture still succeeds and the scanner uses verified Chrome navigation rather than trapping the user in a capture loop.
- Browser navigation remains the authoritative reliability fallback when cookie HTTP is rejected. Authentication failures are now logged with the exact challenge marker or login redirect that caused classification.
- PWA cache is **v2143**.

## Field evidence that created v2.14.3

The v2.14.2 diagnostics showed repeated `POST /api/recu/capture` HTTP 400 responses while the supplied screenshot visibly showed the ordinary Recu homepage. The same diagnostics showed later navigation falsely raising `RecuReauthRequired` before falling back from HTTP 403. The root cause was Reviewer-specific DOM heuristics that were stricter than the already-working Recu Clip Model Counter methodology. v2.14.3 removes those heuristics from Capture and narrows challenge detection to the field-proven four-token set.

## Upgrade

Use `UPDATE_FROM_2_14_2.txt`, restart Reviewer, then fully close/reopen the iPhone PWA once so cache `v2143` is active.

---

# CTBRec Mobile Reviewer v2.14.2

Current release: **v2.14.2 — Recu Proven-Session Capture/Fetch Hotfix**.

## v2.14.2 Recu correction

- Capture Verified Session no longer navigates the dedicated Chrome tab as a prerequisite. It captures the exact Recu Cookie/User-Agent from the already-verified browser profile and persists them immediately.
- Normal Recu scraping is again based first on the long-standing `fetch_recu_html` methodology: persistent cookies, canonical Recu redirect handling, exact legacy header profile, automatic nearby credential-profile recovery, remembered working profile, and bounded 403 retry cooldown.
- Existing v2.14.0/v2.14.1 captured sessions are automatically migrated back to the proven `cookie_http` transport on startup.
- The current Recu tab is inspected in place only as an advisory verification signal; hidden login-form markup cannot override an authenticated shell. Capture never forces a page navigation.
- A failed background HTTP probe after cookie capture is non-fatal. The newly captured session remains stored and the actual scanner/fallback path decides whether access works.
- Chrome navigation remains a last reliability fallback after the proven persistent-session path fails; it is no longer the primary authentication oracle.
- v2.14 incremental per-kink scan memory, stop-at-known-session behavior, and bounded concurrent page scheduling remain intact.
- PWA cache is **v2142**.

## v2.14.1 Recu reliability changes

- Capturing cookies is no longer considered sufficient proof of access. Capture now validates the actual verified Chrome navigation path before saving the session as verified.
- Recu authentication state and background transport state are separate. Failure of experimental CDP `Network.loadNetworkResource` or copied-cookie HTTP no longer means the user must re-authenticate.
- Fast transport order is adaptive: verified CDP when it proves usable; otherwise captured-cookie HTTP with up to 12 concurrent requests; otherwise the reliable verified Chrome navigation fallback.
- Any auth-looking failure from a fast transport is confirmed through the exact verified Chrome navigation session before `needs_verification` is surfaced.
- A valid signed-in Recu page with zero clip tiles is accepted as a legitimate empty listing instead of being misclassified as expired authentication.
- The Settings status now shows whether the session was browser-verified and which transport is active.
- If copied-cookie HTTP is rejected after the verification browser has been closed, Reviewer now launches/attaches the persisted verified profile and confirms access with real browser navigation before asking for re-authentication.
- Desktop direct-original preview now works even when the recording PC opens Reviewer through its Tailscale hostname, correctly supports HTTP suffix/open-ended Range requests, and falls back to an explicit fast-remux URL rather than accidentally retrying the same failing direct path.
- PWA shell cache is **v2141**.

All prior v2.14.0 behavior remains: per-model/per-kink incremental session memory, stop-at-known-session pagination, rate-limit backoff, lightweight navigation fallback, Originals + Review Recu support, and original-file Range preview.

Upgrade from v2.14.0 with `UPDATE_FROM_2_14_0.txt`.
Development continuation requirements are in `CTBRec_Mobile_Reviewer_v2_14_1_HANDOFF.md`.

### v2.14.1 sorting navigation
After Submit/Skip advances to a different chunk, the next mosaic automatically returns to the top. Same-chunk background refreshes do not disturb your scroll position.