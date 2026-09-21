# CTBRec Discovery — Development Handoff

**Current release:** v0.3.0 — Background Collector Foundation  
**Updated:** 2026-09-20  
**Service port:** 8793 (loopback by default)

## 1. Standing user requirements

Every future development response that changes this project must provide:

1. a complete cumulative **FULL SOURCE ZIP**;
2. a safe **PATCH / UPDATE ZIP** for the immediately prior release;
3. this fully current Markdown handoff/context file;
4. a validation/test report and release checksums;
5. an email to `christianpwire@gmail.com` in the existing Gmail thread **CTBRec Discovery Build Updates**.

Gmail blocks the normal source ZIP attachments because they contain script file types. Therefore email the exact ZIP bytes as base64 `.txt` attachments plus the Markdown/test/checksum files. The ordinary ZIPs must still be provided directly in ChatGPT.

Never ship active `discovery_config.json`, SQLite/runtime state, cookies, logs, Recu session material, affiliate WM IDs, passwords/PINs, or other user secrets.

## 2. Product goal

Build a personalized CTBRec discovery/re-identification layer with three distinct jobs:

1. **Model affinity** — discover new public cam-stage accounts the user is likely to value based primarily on their own Favorite / Likely Favorite / Continue / Test / Low Priority decisions.
2. **Public account continuity** — surface likely username/account continuations of already-known models, while keeping merge confirmation explicit and avoiding home-grown biometric identification.
3. **Session opportunity (future)** — identify unusually interesting sessions of already-promising models without contaminating the underlying affinity score with raw popularity.

The system must remain fast, background-safe, explainable and tightly integrated with the existing CTBRec ecosystem without duplicating expensive work.

## 3. Related authoritative applications

### CTBRec Mobile Reviewer / Mosaic Sorter

- loopback server: `127.0.0.1:8787`;
- owns the durable disk/model catalog and mosaic/action workflows;
- `mobile_catalog_cache.json` is the preferred model-universe input;
- Discovery must not create a second recurring recording-library scan;
- existing Reviewer authentication must not be bypassed.

### CTBRec Live Mobile Control

- Java bridge: `127.0.0.1:8791`;
- Python/controller: `127.0.0.1:8792`;
- controls the exact intended running CTBRec instance through the identity-verified JVM bridge;
- already has a proven Chaturbate Users Online v2 affiliate-feed architecture with complete pagination and useful room metadata;
- no normal direct `models.json` writes.

### CTBRec Rapid Model Sorter

- current architecture already maintains local Recu evidence, notably `runtime/recu_local_archive.json`;
- also consumes Mobile Reviewer caches instead of rescanning media;
- Discovery should preferentially read those local outputs rather than launching competing Recu/browser work.

## 4. Release history

### v0.1.0 — initial foundation

Implemented:

- separate Discovery companion service on 8793;
- read-only Mobile Reviewer catalog import;
- SQLite/WAL identity/account/evidence persistence;
- personalized interpretable ranking;
- non-biometric account-continuity matcher;
- JSONL evidence inbox and HTTP evidence API;
- phone-friendly Discover and Possible Returns UI.

### v0.2.0 — integration readiness

Implemented:

- evidence dedupe keys and freshness decay;
- persistent continuity rejection decisions;
- feedback Undo;
- durable inert CTBRec action outbox;
- Reviewer/catalog health adapter;
- broader Mobile Reviewer catalog field compatibility;
- safe patch-upgrade validation.

### v0.3.0 — background collectors (CURRENT)

Implemented:

- optional direct Chaturbate official affiliate Users Online collector;
- local Recu discovery-export collector;
- local neighbor/similarity-export collector;
- local explicit continuity-link collector;
- read-only existing Recu archive enrichment;
- due/polling/source-state manager;
- Sources phone UI and collector APIs;
- new-account freshness signal constrained to the small context score lane;
- monotonic account `first_seen`/`last_seen` merging;
- synthetic pagination/filter/dedupe collector tests.

## 5. v0.3.0 architecture

```text
Mobile Reviewer catalog (RO) ──────────────┐
Rapid Sorter / Reviewer Recu cache (RO) ───┤
Authorized local export files (RO) ────────┼─> CollectorManager ─> SQLite/WAL
Official CB affiliate API (optional) ──────┘                         │
                                                                     ├─> affinity ranker
                                                                     ├─> continuity matcher
                                                                     └─> source/status UI

Discovery feedback ─> action_outbox (execute:false) ─> FUTURE verified bridge adapter
```

Discovery has no code path that executes outbox mutations.

## 6. Persistence

Database defaults to `state/discovery.sqlite3` and uses WAL.

Schema is still version 2:

- `identities` — canonical logical person/model cluster and preference label;
- `accounts` — platform + username records and public metadata;
- `evidence` — source-attributed observations with stable dedupe key;
- `feedback` — preference history/Undo basis;
- `identity_links` — confirmed/rejected account-continuity pairs;
- `source_state` — last attempt/success/cursor/status for collectors/importers;
- `action_outbox` — non-executing future CTBRec sync requests.

v0.3 store behavior now preserves:

- earliest parseable `first_seen`;
- latest parseable `last_seen`;
- existing account source if a lower-level enrichment source observes it later;
- merged metadata fields.

## 7. Current personalized score

Initial weights remain intentionally interpretable:

- `preference_match`: 45%;
- `neighbor`: 25%;
- `recu_momentum`: 15%;
- `context`: 5%;
- `exploration`: 10%.

Positive seed weight derives from the user's graded sorter labels. Low-priority/negative similarity can subtract from preference match.

Neighbor evidence uses an opportunity-normalized shrunk edge approximately `(appearances + 1)/(opportunities + 4)`, then source confidence, seed preference and freshness.

Recu rank/momentum/clip velocity is capped to its 15% lane and freshness-decayed.

`new_account_seen` enters only the 5% context lane (and even there at 60% of that lane) with rapid decay. Viewer/follower counts are stored as metadata only and DO NOT increase affinity in v0.3.0.

## 8. Chaturbate affiliate collector

Code: `discovery/collectors.py::ChaturbateAffiliateCollector`

Endpoint:

`https://chaturbate.com/api/public/affiliates/onlinerooms/`

Parameters:

- user-owned `wm`;
- `client_ip=request_ip`;
- `limit` capped to 500;
- `offset` pagination.

The shipping example config has the collector disabled and WM blank.

Default candidate mode: `new_and_known`:

- import feed rows marked `is_new` as candidate accounts;
- refresh metadata for accounts Discovery already knows;
- ignore ordinary unseen rooms.

Other modes:

- `new_only`;
- `all` (large; deliberately not default).

Explicit non-public states such as private/group/away/offline/password/spy are filtered out. Optional exact gender filtering is supported.

Captured metadata when provided:

- room subject;
- seconds online;
- viewers/users;
- followers;
- age;
- gender;
- location/country;
- HD/new flags;
- image URL;
- current show/state;
- tags;
- spoken languages.

The configured WM ID is never exposed by `/api/collectors` or `/api/health`.

### Important future optimization

Live Control already polls the same affiliate source. The preferred mature architecture is to expose a sanitized complete snapshot from Live Control and let Discovery consume it instead of producing duplicate API traffic. Do this only after the exact current Live Control source/API can be overlaid and validated.

## 9. Local Recu collector boundaries

### `recu_discovery_export`

Reads a local user-authorized JSON/CSV export. Recognizes flexible field aliases for:

- username/model/performer;
- period/window;
- bookmark rank + total;
- recent clip count;
- momentum/percentile change;
- observed timestamp.

Produces stable deduped evidence:

- `recu_bookmark_rank`;
- `recu_clip_velocity`;
- `recu_bookmark_momentum`.

### `recu_local_archives`

Reads configured existing local JSON cache(s), such as Rapid Sorter `runtime/recu_local_archive.json` and Mobile Reviewer `mosaic_lite_recu_cache.json`-style files.

It is **enrichment-only**: unknown accounts are skipped rather than turning every cached Recu performer into a Discovery candidate.

Discovery v0.3.0 DOES NOT launch Recu, capture cookies, bypass verification, or compete with the existing authorized session owner.

## 10. Neighbor collector

`neighbor_export` accepts local JSON/CSV with:

- seed username;
- candidate username;
- evidence source;
- kind (`neighbor`, `rooms_like_this`, `similar_model`);
- appearances;
- opportunities/co-online observations;
- confidence;
- observed timestamp.

This is intended for authorized/public third-party similarity exports and a future approved Rooms Like This observation path. It does not scrape Chaturbate pages itself.

Example file ships under `examples/model_neighbors.example.csv`.

## 11. Continuity collector

`continuity_export` accepts explicit public account links, especially official/public old→new username redirects. It creates `official_redirect` evidence and lets the existing continuity matcher surface a Band A suggestion.

It does NOT auto-merge identities. User confirmation remains required. Rejected pairs persist and remain hidden.

No facial embeddings/recognition are computed by Discovery.

## 12. Collector scheduling and source state

Collector config lives under `collectors` in the active config.

`CollectorManager`:

- runs only enabled + due sources during background sync;
- enforces a minimum 30-second per-source interval;
- serializes runs with a non-blocking lock;
- persists `last_attempt_at`, `last_success_at`, `status`, `detail`, and cursor/signature;
- catches source failures and keeps the Discovery service available.

Local file cursors are `mtime_ns:size` signatures. Unchanged files are skipped.

## 13. v0.3 API/UI

New/expanded GET routes:

- `/api/collectors`;
- `/api/health` includes collector status.

New POST route:

- `/api/collectors/run` with `{"name":"<collector>"}`.

UI tabs:

- Discover;
- Possible Returns;
- Sources;
- Status.

Sources shows enablement, due state, polling cadence, last state/detail, safe configured path(s), and manual Run Now.

## 14. CTBRec mutation boundary

Preference/Undo creates `preference_sync_request` rows with:

- label;
- numeric priority;
- account list;
- target `verified_mobile_reviewer_live_bridge_adapter`;
- `execute:false`.

There is still NO consumer. Do not add one until the exact current Live Control/Reviewer mutation contract is available. Future consumer must preserve exact-target installation fingerprint checks and never use `models.json` as a fallback.

## 15. Configuration / upgrade

FULL source ships only `discovery_config.example.json`. Active `discovery_config.json` must never be included.

v0.2 → v0.3 needs no DB migration. Existing active config remains valid because `load_config()` deep-merges the nested default collector block. All new collectors remain disabled unless explicitly enabled.

## 16. Validation completed for v0.3.0

Development validation includes:

- Python compile/static import checks;
- JavaScript syntax check when Node is available;
- 17 unit/regression tests covering prior v0.2 behavior plus collectors;
- synthetic official-feed pagination, public-state filtering and new-vs-known candidate behavior;
- WM-secret non-disclosure in collector status;
- monotonic first/last-seen merge behavior;
- Recu export file-signature dedupe;
- neighbor evidence personalization;
- explicit continuity export producing Band A suggestion;
- local Recu archive enrichment without universe expansion;
- HTTP smoke tests for health/recommendations/collectors/static UI;
- package validator safety gates;
- patch overlay test from exact v0.2.0 full source with seeded active config/runtime files preserved byte-for-byte;
- fresh-extraction full-package revalidation.

See `TEST_REPORT_v0_3_0.txt` for exact results.

## 17. Known limitations

- No v0.3 direct Live Control snapshot reuse yet; optional direct affiliate polling can duplicate Live Control's network request if both are enabled.
- No built-in Recu bookmark leaderboard acquisition. v0.3 consumes a local authorized export because Discovery must not create another Recu auth/browser stack.
- No automatic third-party neighbor fetcher yet. v0.3 establishes the normalized import contract first.
- No session-opportunity/anomaly engine yet.
- No outbox execution/live CTBRec mutation yet.
- No automated official redirect checker yet; redirect evidence currently arrives through the continuity export/API.
- Development environment cannot perform real-PC integration tests against the user's running Reviewer/Live Control/CTBRec instances.

## 18. Exact recommended next build

### v0.4 — ecosystem-native collectors + session opportunity foundation

Priority order:

1. Inspect/export the exact current **Live Control v1.0.20+ server source** and add a sanitized read-only affiliate snapshot endpoint or local snapshot file, so Discovery can reuse its existing complete Users Online fetch instead of making a duplicate request.
2. Inspect the exact current **Rapid Sorter / Recu archive shape** and write dedicated parsers for bookmark/clip/ranking information actually available there rather than only generic flexible fields.
3. Add a local candidate-observation history table/aggregator for repeated online observations and schedule/tag trajectories without storing every raw polling row forever.
4. Add the separate **Session Opportunity** score: self-normalized viewer/follower/tip/clip anomalies relative to the model's own baseline. It must not change the affinity score.
5. Add an evaluation/audit view showing recommendation outcomes by source and component, preparing for learned weights after enough feedback exists.
6. Only after exact bridge source is available, implement an idempotent verified outbox consumer through the existing Live Control identity-verified bridge.

## 19. Files of interest

- `discovery/collectors.py` — v0.3 source ingestion layer;
- `discovery/store.py` — SQLite/WAL persistence and dedupe;
- `discovery/scoring.py` — affinity scoring;
- `discovery/continuity.py` — public-account continuation matcher;
- `discovery/engine.py` — sync/orchestration/background loop;
- `discovery/integration.py` — safe Reviewer probe/catalog boundary;
- `discovery/server.py` — HTTP API/static server;
- `static/` — phone UI;
- `tests/test_core.py` — regression tests;
- `validation/validate_package.py` — shipping safety gates;
- `examples/` — collector import examples.
