# Self-directed development prompt — CTBRec Mobile Reviewer v2.13 Performance + Reliability

Continue from the verified v2.12.0 package. Treat the user's complaint as a whole-program performance/reliability defect, not a one-off UI tweak.

## User-observed failures to eliminate
1. iPhone/PWA intermittently appears to crash/restart and asks for the fallback PIN again.
2. Switching among Originals / Review / Easy Sort / Hidden / cleanup / deletion is extremely laggy and may continue showing a previous tab's model list.
3. Tapping a model can take far too long before the first sortable mosaic appears.
4. Sorting interactions themselves must remain responsive regardless of background work.
5. Mosaic generation is inefficient because it repeatedly launches ffmpeg once per sampled image; reduce process-start overhead by extracting multiple requested frames per ffmpeg process where safe.
6. Search for adjacent inefficiencies throughout desktop/server/mobile code and remove them without compromising destructive-safety rules or the verified v2.12 frame-cut behavior.

## Required investigation
- Audit client request races, stale responses, DOM churn, large-list rendering, polling, full-current refreshes, service worker behavior, auth/bootstrap failure handling, IndexedDB/service-worker interactions, and redundant rerenders.
- Audit server hot paths used by tab switching, model-list retrieval, ready counts, model-admin filters, model opening, current payload construction, sidecar/image validation, JSON transfer size, HTTP connection behavior, and background lock contention.
- Audit ffmpeg/ffprobe launch patterns in Originals and Review mosaic generation. Preserve exact sampling semantics and sidecar/tap-map correctness.
- Search for operations repeated on every tab switch/tap that can be cached or moved off the latency-critical path.
- Preserve the user's absolute UX rule: ready sorting UI must never wait on Recu, live CTBRec controls, background scanning, queued filesystem work, whole-library mosaic generation, or unrelated status refreshes.

## Required fixes / design goals
### Authentication / crash resilience
- Never show the PIN screen merely because bootstrap/catalog/network initialization had a transient failure. Distinguish "not authenticated" from "temporarily unreachable/failed request".
- Keep a durable same-installation paired-device credential in addition to the HttpOnly cookie, so iOS PWA cookie loss does not needlessly force a PIN re-entry. It must be installation-bound and invalid if the server secret changes.
- Add request timeouts/retry behavior appropriate for idempotent GETs; do not replay destructive mutations automatically.
- Keep an already rendered/sortable screen usable during transient background/API failures.

### Model-list / tab performance
- Prevent stale/out-of-order catalog responses from overwriting the currently selected tab.
- On tab switch, immediately change/clear the visible list or show a cached target-tab list; never leave the old tab's list looking authoritative while a request is in flight.
- Cache model catalogs client-side and server-side with safe invalidation/short TTL.
- Avoid creating thousands of model-card DOM nodes at once. Use progressive/windowed rendering with incremental batches and an IntersectionObserver or equivalent.
- Search should remain correct across the whole fetched catalog.
- Optimize Easy Sort/Hidden/Ignore lookup so models.json/sidecar files are not reparsed from disk on every catalog request.
- Compress large JSON responses and use HTTP/1.1 keep-alive where safe.

### Model-open / sorting performance
- The open-fast path should validate only enough ready mosaics to display immediately (small bounded warm set), then hydrate more in the background.
- Do not synchronously scan/recover every mosaic sidecar on a phone tap when the ready index is absent; schedule recovery/build asynchronously.
- Replace the 2.2s full `/current` polling path with a lightweight status endpoint. Fetch the full current payload only when the chunk or mosaic layout actually changed.
- Cache validated embedded mosaic layouts in memory so repeated status/UI calls do not reopen every JPEG just to re-check dimensions.
- Minimize rerenders after tile taps; update only affected tiles/file rows and summary where practical. Avoid O(files × frames) recomputation in frame-cut mode.

### Mosaic generation efficiency
- Add a multi-frame extraction helper that groups multiple seek requests for the same source into one ffmpeg process (bounded batch size, e.g. 8) using multiple independently seeked input instances in one invocation, producing several output JPEGs.
- Preserve exact per-frame requested timestamps. Do NOT replace exact sampling with an approximate fps filter.
- Keep hard timeouts and cancellation. If a batch fails or produces missing outputs, fall back only for the missing frames to the existing single-frame extractor so one odd seek cannot invalidate the whole mosaic.
- Apply this to both Originals and Review mosaic generation.
- Keep CPU/disk bounded; batch size must be configurable/default conservative, and mobile/background responsiveness must still have priority.

## Safety / compatibility constraints
- Preserve v2.12 precise frame-cut semantics, durable queue, verified-output-before-original-deletion invariant, Back/Undo behavior, and idle-only priority.
- Preserve v2.11 live-bridge identity verification and never write models.json for CTBRec-native mutations.
- Preserve v2.11.3 background-liveness behavior.
- Never delete or overwrite user config/state/recording roots/Recu session/catalog/queues in the drop-in package.
- Existing mosaics and sidecars must remain reusable.
- No hidden destructive fallbacks.

## Validation requirements
Create targeted automated/synthetic tests or executable checks for at least:
1. transient bootstrap/catalog failure does not route an authenticated user to PIN;
2. auth fallback token is accepted and installation-bound;
3. stale model-list response cannot overwrite a newer tab selection;
4. model rendering is bounded (initial DOM count far below 4,100) and can progressively load more;
5. server model catalog cache returns equivalent rows and invalidates on admin/catalog changes;
6. Easy Sort lookup no longer reparses unchanged legacy files on every request;
7. lightweight queue status endpoint does not build/read mosaic layout geometry;
8. open-fast validates only a bounded number of ready mosaics before returning;
9. multi-frame ffmpeg extraction launches fewer processes than per-frame extraction and generates all expected images; missing batch outputs fall back safely;
10. Python compilation and JS syntax checks pass;
11. a seeded v2.12 install retains active config/state files byte-for-byte after overlay.

## Deliverables
- Small v2.13 performance/reliability hotfix ZIP for an existing v2.12 install.
- Cumulative v2.13 drop-in ZIP.
- Complete up-to-date v2.13 full package including the verified bundled NudeNet model.
- Updated README/changelog/update instructions.
- Updated Markdown handoff/context file that accurately describes current architecture, completed work, known issues, validation results, exact next steps, and includes this self-directed prompt so a future chat can continue without losing context.
- Test report and SHA-256 checksums.

Execute this prompt now. Think ahead, inspect actual code rather than assuming causes, and prioritize the phone's interactive sorting lane above all background work.
