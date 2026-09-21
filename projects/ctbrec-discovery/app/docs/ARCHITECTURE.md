# CTBRec Discovery v0.4.1 Architecture

## v0.4.1 field-adapter delta

- Mobile Reviewer catalog walking is recursive across mode/root containers and de-duplicates real model identities.
- Catalog and Recu cursor generations are bumped (`v41:` / `v41|`) for a one-time corrective reparse.
- Collector exclusion is per-source rather than global.
- Reviewer health prefers lightweight `GET /api/auth`; no whole-shell GET probe.
- Status distinguishes outer catalog buckets from unique model count.
- All v0.4 startup/background, eligibility, inert-outbox and local-first invariants remain.

---


## Core invariant

Discovery may be unavailable, stale or externally rate-limited without ever preventing the user from sorting recordings or controlling CTBRec. It runs as a separate companion process on port 8793, consumes caches/APIs read-only, and writes only its own SQLite/WAL state plus inert future-action intents.

No Discovery path recursively scans the recording archive.

## Process topology

```text
Mobile Reviewer 8787
  mobile_catalog_cache.json ───────────────┐
                                            │
Live Control 8792                           │
  /api/models (loopback only) ─────────────┤
                                            ├─> Discovery 8793
Rapid Sorter / Reviewer local Recu caches ─┤     SQLite/WAL
                                            │       │
Optional local evidence exports ───────────┤       ├─> affinity ranker
                                            │       ├─> continuity matcher
Manual official affiliate fallback ────────┘       └─> execute:false outbox
```

The exact-target JVM bridge on 8791 remains owned by Live Control/Reviewer integrations. Discovery does not invent a second CTBRec mutation channel.

## Startup lifecycle

v0.3 performed local/source synchronization inside `App.__init__` before binding the HTTP socket. When the direct affiliate collector was enabled, first startup could therefore show a blank command window for minutes.

v0.4 changes ordering:

1. parse config;
2. open Discovery SQLite;
3. construct the engine/collectors **without running them**;
4. bind `ThreadingHTTPServer` on 8793;
5. print the listening URL;
6. start the engine background thread;
7. perform initial sync asynchronously.

`request_sync()` also runs in a background thread. A process-local sync lock prevents overlapping full sync passes.

## Mobile Reviewer catalog adapter

The field-observed cache shape is:

```json
{
  "updated_at": "...",
  "roots_file": "...",
  "catalog": {
    "username": [
      {"folder": "...", "bytes": 123, "...": "..."}
    ]
  }
}
```

v0.4 treats the `catalog` map key as the canonical username when child rows do not carry it themselves. List-valued entries are aggregated into one Discovery account observation. Aggregation can preserve/derive folder count, total bytes, file count, per-drive bytes and useful seen metadata when present.

The source cursor is `v4:<mtime_ns>:<size>`. Once that exact file version has been imported, the next 60-second local sync returns unchanged without reparsing the multi-megabyte JSON.

The catalog remains a read-only input; Discovery never updates it.

## Live Control reuse

`LiveControlModelsCollector` calls only the configured local `live_control_base_url`, normally `http://127.0.0.1:8792/api/models`.

Its purposes are to recover current exact-target CTBRec workflow priority/favorite information for models Live Control exposes, recover online/public state, and reuse `affiliateRoomInfo` already merged by Live Control, including gender/tags/subject/viewers/followers/live duration where present.

This collector makes **zero Chaturbate requests** itself.

### Important global-feed limitation

The current Live Control API surface contains tracked CTBRec models. Its handoff documents a complete affiliate snapshot internally, but the current public local API does not expose that entire untracked global room universe. Discovery therefore cannot truthfully obtain all newly-online/new-account candidates from `/api/models` alone.

v0.4 policy:

- scheduled standalone global affiliate polling: off by default;
- known/tracked metadata: reuse Live Control every ~60 seconds locally;
- full official affiliate sweep: explicit one-shot fallback when a WM ID is configured;
- future preferred architecture: add a sanitized complete-snapshot local endpoint to Live Control, then remove duplicate global fetching entirely.

## Recu local archive adapter

The collector remains strictly local-file based. It recognizes map-style caches such as:

```json
{
  "version": 1,
  "models": {
    "username": {
      "fetched_at": "...",
      "moments": [...],
      "video_meta": {...}
    }
  }
}
```

For each parsed record it records diagnostics: parsed records, matched known accounts, skipped unknown accounts, imported/enriched records and per-file errors/details.

`moments` and `video_meta` are currently summarized into account metadata (`recu_moments`, `recu_recordings`). This source does not yet generate bookmark-rank/momentum evidence unless those signals are supplied by the separate discovery export.

The v0.4 cursor is version-prefixed so the first v0.4 run reparses files that v0.3 may previously have marked as seen even though it failed to understand their schema.

## Recommendation eligibility

Eligibility occurs before ranking. Default config:

```json
{
  "allowed_genders": ["m"],
  "include_unknown_gender": true,
  "include_couples": false
}
```

Known female/couple accounts therefore do not appear by default. Unknown is allowed because many local historical accounts lack current affiliate metadata; otherwise the filter would incorrectly discard potentially relevant candidates merely due to missing data.

The browser Setup panel can update this section at runtime. The settings endpoint never returns WM IDs or credentials and writes only the recommendation-filter object into the active config while preserving all other keys.

## Preference semantics

Taste weights:

- Favorite: strong positive;
- Likely Favorite: positive;
- Continue: positive;
- Test: **neutral in v0.4**;
- Unsorted: neutral;
- Low Priority / Ignore: negative where applicable.

`Test` still maps to its normal CTBRec workflow priority for future synchronization. Only its role as a recommender training label changed. Operational states such as offline/suspended are not taste negatives.

## Score semantics

The total is a 0-100 ranking index, not a calibrated probability.

Current lanes: 45 preference/tag similarity, 25 neighbor evidence, 15 Recu rank/momentum/clip evidence, 5 context and 10 controlled exploration.

The neutral preference baseline is intentionally not presented as confidence. Recommendation results separately expose `confidence`, reasons and `recommendation_evidence_level`.

## Sync/state API

New/important routes:

- `GET /api/sync` — queue one background full sync; returns immediately;
- `GET /api/sync-status` — current running/last outcome;
- `GET /api/diagnostics` — counts by preference/gender/evidence + filters/sync state;
- `GET /api/settings` — safe non-secret settings only;
- `POST /api/settings` — recommendation filters only;
- `GET /api/collectors` — source status/config-safe details;
- `POST /api/collectors/run` — explicit one-source run.

Existing evidence/feedback/continuity/outbox APIs remain.

## Reviewer reachability probe

Some Python HTTP handlers return 501 for `HEAD` even when the service is healthy. v0.4 tries `HEAD /`, then falls back to a bounded `GET /` for 405/501 before declaring the Reviewer unavailable. 401/403 is treated as reachable with an authentication boundary rather than offline.

## Media integration boundary

Live Control already owns on-demand room thumbnail/HLS resolution and the exact tracked-model security boundary. Discovery v0.4 does **not** copy that resolver.

Next implementation should either add a narrow authenticated local bridge/proxy from Discovery into Live Control's existing media endpoints or factor a shared media broker used by both apps.

Likewise, Rapid Sorter's local `runtime/recu_best_moments/` should be indexed and range-streamed locally before any live Recu request is considered.

## Persistence

SQLite schema remains v2: identities, accounts, evidence with stable dedupe keys, feedback, identity links/rejections, source state and action outbox.

PATCH upgrades never ship or overwrite the user's active database/config/runtime state.

## Safety invariants

1. Discovery failure never blocks sorting.
2. No second recording-library scanner.
3. No direct normal `models.json` writes.
4. No guessed bridge commands.
5. No face recognition/biometric matching.
6. No Recu access-control bypass or competing browser-session owner.
7. No Chaturbate performer-page scraping loop.
8. Network collectors are bounded and independently disable-able.
9. Known-model Live Control refresh is loopback only.
10. Action outbox remains inert until an exact identity-verified consumer is implemented and tested.

## Git/release bridge (v0.4.2+)

Discovery source includes `tools/git_bridge/`, a project-specific bridge for the umbrella `fcd02/Chaturbate_Projects` repository. It keeps public source under `projects/ctbrec-discovery/app`, private runtime configuration/state outside Git, and automates validated future PATCH publication/deployment. The bridge is intentionally downstream of release packaging: it consumes a validated PATCH ZIP, re-validates it in the repo copy, publishes through a release PR/tag, then deploys that same payload to the live runtime with touched-file backup and health-gated rollback.
