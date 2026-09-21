# Engineering Prompt — v2.15.9 Preview Seek & Geometry Stability

You are maintaining CTBRec Mobile Reviewer/VaultFlow. Work from the current stable v2.15.8 source and preserve every working behavior unless explicitly changed below. Treat the two supplied Preview Diagnostics bundles as authoritative evidence.

## Evidence

Smooth diagnostic (`317dbe24bb05`):
- source is a real MP4 container (`mov,mp4,...`) with H.264 Main + AAC
- Chrome direct playback succeeds immediately
- normal HTTP Range requests are used
- repeated seeks complete in roughly tens to low hundreds of milliseconds

Slow diagnostic (`48fec12709cf`):
- source is a true MPEG-TS container with H.264 Main + AAC
- Chrome rejects direct playback with `DEMUXER_ERROR_COULD_NOT_OPEN`
- current live zero-reencode fragmented-MP4 remux terminates after ~0.19 s instead of the ~750 s source
- compatibility transcode works but every seek restarts FFmpeg and takes roughly 2–3 s before playback resumes

The codecs in the failing TS are already browser-friendly, so do not transcode merely because the container is MPEG-TS.

## Required behavior

1. Add a persistent temporary **seekable preview cache** for sources that cannot be used directly by the browser.
   - Produce a real MP4 file using FFmpeg stream copy (`-c copy`) only; do not re-encode.
   - Key cache identity by source path + size + mtime so stale wrappers are never reused after the source changes.
   - Materialize atomically via a temporary file.
   - Validate that the output is nontrivial and, when source duration is known, is not truncated. A ~0.19 s wrapper of a ~750 s source must be rejected.
   - Serve the completed cached MP4 through the existing correct HTTP Range implementation so seeking behaves like ordinary MP4 playback.
   - Reuse the cache on later previews/seeks and across Reviewer restarts while still fresh.
   - Bound cache lifetime and total size; deleting cache files must never touch source recordings.
   - Record build/cache-hit/failure telemetry in Preview Diagnostics.

2. Optimize the automatic playback ladder.
   - Browser-native MP4/M4V/MOV on desktop: original-file direct Range playback first.
   - True TS/MPEGTS/M2TS/MTS and other non-native containers: skip the known-bad raw-browser attempt and use seekable zero-reencode cached MP4 first.
   - If native direct/HLS fails, try the seekable cached MP4 before compatibility transcode.
   - If seekable remux cannot be created/played, automatically fall back to Compatibility.
   - Preserve the rule that a playback-method failure never advances to the next selected recording.
   - Preserve full source duration and source-relative seeking.

3. **Preview geometry must never jump when playback mode changes.**
   - Keep a stable player viewport height/width through Direct, HLS, seekable remux, streaming remux, and Compatibility.
   - Use `object-fit: contain` inside that fixed viewport so differing resolutions/aspect ratios do not resize the modal.
   - Reserve stable space for the status/note area and mode action row.
   - Do not remove/reinsert controls in a way that moves Prev/Next or the scrubber while the user is interacting.
   - Switching methods may change text/state, but not the overall preview control geometry.

4. Preserve all unrelated functionality from v2.15.8 exactly: mosaic generation/reuse, READY/index semantics, active-model look-ahead, frame cuts, Shift selection, TS-only model sorting, Recu, Keep Last, action queue, Git Bridge, runtime/private-state preservation, and diagnostics export.

5. Add regression coverage that verifies:
   - actual H.264/AAC MPEG-TS can be stream-copy remuxed to a near-full-duration MP4 and the cached result is reused;
   - a truncated remux is rejected;
   - the new playback ladder prefers seekable cache for non-native desktop containers and direct Range for native MP4;
   - preview viewport/control geometry remains constant when stages/controls change in Chromium;
   - all inherited tests remain green.

6. Bump release/cache identity to v2.15.9, update changelog/handoff/test report, create a safe patch that excludes runtime/private state, and validate both the final full-source ZIP and patch overlay before delivery.

Favor simple, observable, recoverable behavior. Do not hide failures: diagnostic exports should make it obvious which stage failed and why.
