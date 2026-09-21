# CTBRec Discovery — Complete Development Handoff

**Current release:** v0.4.5 — Authoritative Current-Release Validation / Git Bridge v1.1.0
**Updated:** 2026-09-20
**Service port:** `127.0.0.1:8793` by default
**SQLite schema:** v2 (unchanged)
**Upgrade base:** v0.4.4; PATCH is the primary install artifact, source/docs/tests/tooling only, and must preserve active config + state. Historical validators may be present or absent and are never auto-executed.

## 1. Non-negotiable delivery requirements

Every development delivery for this project must include:

1. **final PATCH / UPDATE ZIP first** — this is the user's normal install path and must be regenerated after every final source/doc/test/handoff edit;
2. complete cumulative **FULL SOURCE ZIP**;
3. fully current Markdown handoff/context file;
4. validation report + SHA-256 checksums.

**Do not email releases.** Git/Git Bridge is the delivery/history mechanism unless the user explicitly asks for email again.

The exact final PATCH ZIP must be overlay-tested against the immediately prior field release with seeded `discovery_config.json`, SQLite/state, and unrelated private-runtime sentinels; those files must remain byte-for-byte unchanged. The exact final FULL SOURCE ZIP must be freshly extracted and package-validated.

Never ship active `discovery_config.json`, SQLite/runtime state, logs, cookies, Recu session/browser-profile material, Chaturbate WM IDs, PINs/passwords/secrets, local recordings/media, or unrelated user-private runtime state.

The mandatory release-engineering process is also frozen in `docs/RELEASE_ENGINEERING_PROMPT.md`.

## 2. Product goal and three-engine separation

Discovery is a personalized companion to the user's CTBRec ecosystem. Keep these tasks separate:

1. **Model affinity** — who the user is likely to value, using their own Favorite/Likely Favorite/Continue/Low Priority/Ignore history as the anchor.
2. **Account continuity** — whether a new public account likely continues a known model identity, using public/non-biometric evidence and explicit user confirmation.
3. **Session opportunity** — future ranking of unusually interesting live sessions for already-promising models. Viewer/tip anomalies belong here, not in base model affinity.

The service must stay explainable, reversible, background-safe, and reuse existing local state/services rather than creating competing scans or sessions.

## 3. Neighboring applications / authoritative ownership

### Mobile Reviewer / Mosaic Sorter

- user's current field server identified itself as `CTBRecMobile/2.15.5 Python/3.10.11`;
- loopback port `8787`;
- owns recording-disk catalog/mosaic/action workflows;
- `mobile_catalog_cache.json` is Discovery's preferred read-only disk-model universe;
- Discovery must never add another recurring multi-TB library scan;
- Reviewer hot path must never wait for Discovery.

Older Reviewer handoffs explicitly document that `mobile_catalog_cache.json` is a persisted disk catalog loaded into memory and used by `/api/catalog`; it exists to avoid repeated whole-library scans.

### Live Mobile Control

- JVM bridge `127.0.0.1:8791`;
- Python/web service `127.0.0.1:8792`;
- authoritative for exact-target live CTBRec mutations;
- already owns robust Chaturbate Users Online v2 affiliate polling and last-good state;
- `/api/models` is a local tracked-model surface and includes public affiliate room metadata when available;
- proven live room preview/player stack already exists and should be reused later rather than duplicated.

### Rapid Model Sorter

- consumes Mobile Reviewer state rather than creating a second library scanner;
- local Recu stats: `runtime/recu_local_archive.json`;
- local Best Moments: `runtime/recu_best_moments/`;
- existing Recu auth/recovery remains owned by Rapid Sorter/Mobile Reviewer, not Discovery.

The older Mosaic Recu cache shape is also confirmed as a top-level `models` mapping containing per-model moments and metadata.

## 4. Release history

### v0.1.0

Foundation: local service, SQLite/WAL identities/accounts/evidence, first interpretable ranker, evidence inbox/API, continuity matcher, Discover + Possible Returns UI.

### v0.2.0

Evidence dedupe/freshness, feedback Undo, persistent continuation rejects, integration probes, inert action outbox (`execute:false`).

### v0.3.0

Optional official Chaturbate affiliate collector; local Recu/neighbor/continuity import surfaces; first real field-testable Sources page. Startup still incorrectly ran due collectors before the server listened.

### v0.4.0

Nonblocking startup; Setup UI/gender eligibility; Test neutral in preference learning; first Live Control `/api/models` reuse; attempted real Mobile Reviewer/Recu schema adapters; asynchronous Sync; `CHECK_CONFIG.bat`.

### v0.4.1

Field-adapter hotfix driven by the user's actual v0.4 Status output. Fixes four concrete production defects without expanding risky feature scope.

### v0.4.2

Git Bridge integration. Adds a Discovery-specific, public-safe Git/deployment bridge modeled on the existing Mobile Reviewer bridge. Default repository remains `fcd02/Chaturbate_Projects`; Discovery lives at `projects\ctbrec-discovery\app`. The bridge bootstraps the current validated runtime into Git, keeps private/runtime state outside Git, and automates future PATCH validation, commit, PR, squash-merge, version tag, live deployment, health verification and rollback.


### v0.4.3

Git Bridge field-recovery hotfix. The first real Windows setup attempt authenticated and cloned correctly, then `validate_v040.py` failed. Because bridge v1.0 had already written `.gitignore`, `PROJECTS.md`, and `projects/ctbrec-discovery/`, its `.cmd` wrapper immediately retried the setup with a second Python invocation and that second run failed on the now-dirty working tree. Remote GitHub remained unchanged. v0.4.3 makes setup transactional, self-recovers only unmistakable untracked Discovery bootstrap artifacts from that failed run, surfaces validation output, and fixes the launcher retry bug.

### v0.4.4

Git Bridge incremental-upgrade validation hotfix. The second real Windows setup attempt correctly recovered the prior local bootstrap artifacts and validated in a temporary staging directory, but `validate_package.py` failed because it required `validation/validate_v041.py`. The user's live installation had legitimately been assembled through PATCH overlays where unchanged historical validators were not guaranteed to be present. That exposed a bad release invariant: a current installation must be valid based on current source/runtime requirements, not on possession of every historical validation script. v0.4.4 changes validation to current-release semantics, makes historical validators optional, dynamically executes whichever historical/current validators are present, and intentionally ships the complete validation directory in this repair PATCH to self-heal the user's current tree. Git Bridge version is now 1.0.2.


### v0.4.5 — CURRENT

Authoritative-current validation redesign. The third real Windows bootstrap attempt showed that v0.4.4 still auto-executed every historical `validate_vXYZ.py` file that happened to be present. `validate_v041.py` is an old integration contract and failed against newer valid source (`imported` expectation 4 vs current observed 3 in that stale scenario). That proved historical validators cannot safely be treated as forward-compatible test suites.

v0.4.5 replaces glob-based historical execution with a single explicit validation contract:

- `validation/release_manifest.json` declares release `0.4.5`, current suite `validate_current.py`, and `validate_package.py`;
- `validation/runner.py` is the only automatic validation entrypoint for Git Bridge and `scripts/VALIDATE.bat`;
- historical `validate_v040.py` … `validate_v044.py` remain archival/reproducibility assets but are **never auto-run** against newer source;
- Git Bridge v1.1.0 calls only the authoritative runner for both package staging and live-runtime validation;
- `validate_current.py` covers current nested-catalog behavior, local Recu/Live Control/Reviewer integration, recommendation gender filtering, HTTP/secret boundaries, and validation/bridge invariants;
- `validate_package.py` checks manifest/version consistency and explicitly forbids Git Bridge from glob-running historical validators.

This is an architecture correction, not another one-off assertion change. The field-failing `validate_v041.py` is intentionally left in the tree as archival evidence. Whether any historical validator happens to pass or fail against newer source is irrelevant: v0.4.5 validation must not execute it automatically. Release testing additionally injects a deliberately failing synthetic historical validator to prove that stale historical files cannot affect the current release result.

## 5. Exact v0.4 field observations that created v0.4.1

The user's v0.4 Status reported:

- `live_control_models`: **enabled**, `due:true`, but `status:never_run`; clicking Run now returned **`live control models: busy`**;
- Mobile Reviewer catalog file existed, size ~2.2 MB, shape object, top-level keys `updated_at`, `roots_file`, `catalog`, but Discovery reported **`3/3 imported`** and `catalog_entry_count: 3`;
- local Recu source found both configured files with zero parse errors but reported **`imported: 0`**;
- Reviewer service probe had previously reached `CTBRecMobile/2.15.5`, then v0.4 reported a timeout/offline condition;
- outbox had 4 pending rows, which is expected from prior feedback testing and remains inert.

Interpretation:

1. the real catalog's `catalog` member has three **outer structural buckets**; v0.4 treated those keys as usernames instead of descending into them;
2. because the real known account universe was never imported, the known-only Recu enricher had almost nothing to match, making its `imported:0` unsurprising;
3. one global CollectorManager lock meant a long local Recu parse could make an unrelated Live Control manual run return `busy` before it ever contacted port 8792;
4. v0.4's Reviewer fallback from unsupported `HEAD /` to a whole-shell `GET /` could time out and falsely label an otherwise healthy Reviewer offline.

## 6. v0.4.1 Mobile Reviewer catalog repair

Primary code: `discovery/importers.py`.

### Prior defect

v0.4 special-cased:

```text
catalog[key] -> one model account
```

That assumption was correct only for one older/direct-map shape. In the user's current cache, the three `catalog` keys are structural buckets (typically mode/root groupings), so it created three meaningless accounts and stopped.

### Current schema-adaptive walker

`_walk_catalog()` recursively handles:

- `catalog -> original/review/deletion -> list rows with explicit model names`;
- `catalog -> mode -> username -> [folder rows]`;
- `catalog -> root path -> username -> row/list`;
- direct legacy `catalog -> username -> [folder rows]`;
- generic `models/items/entries/data/rows` wrappers.

A dictionary key is only treated as a username when its child proves it is a model/folder record. Known structural/mode/root/path keys are recursively descended.

Duplicate rows for the same platform+username are aggregated before upsert, so a model appearing in Original + Review does not become duplicate identities. Size/file/folder/per-drive metadata is merged where available.

### Forced one-time reparse

Catalog source cursor changed from:

`v4:<mtime>:<size>`

to:

`v41:<mtime>:<size>`

Therefore a user's unchanged cache that v0.4 falsely marked successful is **automatically reparsed once** after installing v0.4.1.

### Conservative cleanup of v0.4 bucket artifacts

`DiscoveryStore.remove_safe_import_artifact_account()` can remove a v0.4-generated outer bucket account only when all are true:

- account source is exactly `mobile_reviewer_catalog`;
- identity preference is `unsorted`;
- identity has exactly one account;
- no evidence references the account;
- no feedback exists for the identity;
- no outbox action references it;
- no identity-link history references it.

This allows harmless `original` / `review` / `deletion`-style artifacts to disappear without ever deleting user-reviewed or evidence-bearing state.

## 7. v0.4.1 Recu reparse and diagnostics

Primary code: `discovery/collectors.py`, `RecuLocalArchivesCollector`.

The local collector remains **read-only** and makes no Recu web request. It recognizes top-level `models`/performers/items/data/cache/records wrappers plus direct username maps. It only enriches accounts already known to Discovery; it does not turn a giant Recu archive into an unlimited candidate universe.

The cursor prefix changed from `v4|` to `v41|`. This intentionally forces a one-time parse of the same local files after the catalog universe is repaired.

Expected detail after first v0.4.1 run:

```json
{
  "files": 2,
  "parsed_records": <nonzero>,
  "matched_known": <nonzero if names overlap>,
  "skipped_unknown": <possibly large>,
  "imported": <matched useful rows>,
  "errors": 0,
  "per_file": {...}
}
```

If `parsed_records` remains zero, the local cache schema is still unsupported. If parsed is large but `matched_known` is zero, the catalog/account universe is still wrong or usernames differ. This separation is deliberate.

## 8. v0.4.1 collector concurrency fix

Primary code: `CollectorManager` in `discovery/collectors.py`.

### Prior behavior

All collectors shared one `_lock`. A large local Recu parse could block:

- Live Control loopback collector;
- manual source buttons;
- unrelated local file collectors.

The UI then reported only `busy`, even though Live Control itself had never been contacted.

### Current behavior

CollectorManager has one lock **per collector name**.

- same source cannot run twice concurrently;
- Live Control can run while Recu local parsing is active;
- a `busy` response now includes `busy_source` and means **that same source already has an in-progress run**;
- source status includes `running:true/false`, and the UI labels/disables an actively running source accordingly.

SQLite remains WAL-backed with separate short-lived connections and a busy timeout, so independent local collector writes remain serialized safely by SQLite rather than by an application-global mutex.

## 9. v0.4.1 Reviewer health probe fix

Primary code: `discovery/integration.py`.

### Prior behavior

v0.4 did `HEAD /`; if it received 405/501, it fell back to `GET /` and read the web shell. On a busy Reviewer this could exceed the short probe timeout and create a false `offline` status.

### Current behavior

Probe order:

1. `GET /api/auth` — lightweight local endpoint already present in supported Reviewer lines;
2. 200 => reachable;
3. 401/403 => reachable + auth boundary intact;
4. only if `/api/auth` is genuinely unavailable (404/405/501), fallback to `HEAD /`;
5. never fetch the full HTML shell as a health probe.

The code grants at least 2 seconds to this local probe even if an older active config still contains v0.4's 1.25-second setting. New defaults use 2.5 seconds.

## 10. Catalog Status diagnostics corrected

`integration.catalog_file_status()` no longer calls the outer three buckets `catalog_entry_count` as though they were model records.

It now reports:

- `catalog_outer_bucket_count`;
- `catalog_outer_keys_sample`;
- `catalog_model_row_groups` from the same schema-adaptive walker;
- `catalog_unique_model_count`.

For the user's library, the important field after v0.4.1 is `catalog_unique_model_count`; it should be in the realistic thousands-range rather than `3`.

## 11. Current ranking semantics

The displayed number is a **0–100 ranking index**, not a probability.

Current lanes remain approximately:

- 45% preference match;
- 25% neighbor evidence;
- 15% Recu momentum;
- 5% context;
- 10% controlled exploration.

`Test` is neutral/uncertain, not positive taste evidence. Global default eligibility:

- male: included;
- female: excluded;
- couples: excluded unless explicitly enabled;
- unknown gender: included so incomplete metadata does not silently erase candidates.

Recommendation quality is not yet considered mature while neighbor evidence and Recu bookmark/momentum exports are absent. v0.4.1's goal is to make the **real catalog + local Recu + Live Control preference seeds** actually available to the ranker first.

## 12. Chaturbate affiliate behavior

Direct Discovery affiliate scheduled polling stays disabled by default.

The user's v0.3 database retains one historical official-feed sweep (12 pages / 5,984 rows / 99 new candidates / 250 known refreshed). Keeping the collector disabled does not delete that evidence.

Current preferred architecture is local reuse of Live Control. `/api/models` is tracked-only, so it is not yet a complete replacement for the global new-account universe. Do not claim duplicate global polling is fully solved until exact current Live Control source is extended with a sanitized accepted full-snapshot contract.

No Chaturbate performer-page scraping is used.

## 13. Recu access boundary

Discovery v0.4.1 reads configured local JSON/cache files only. It does **not**:

- log into Recu;
- capture cookies;
- navigate verification;
- bypass Cloudflare/access controls;
- compete with Rapid Sorter/Mobile Reviewer for the authorized browser profile.

Rapid Sorter's local Best Moments remain the preferred media source for future Discover playback.

## 14. Account continuity boundary

Continue to prioritize:

1. official/public redirect/linkage;
2. confirmed external rename evidence;
3. non-biometric username morphology + temporal/schedule/language/tag/public-profile continuity;
4. explicit user confirmation before weak/medium merges.

Do not add automated face biometric identification.

## 15. Persistence / mutation boundary

Discovery decisions are stored in its own SQLite DB. `queue_preference_actions:true` creates durable outbox rows with `execute:false`.

There is still **no action consumer** in v0.4.1. Discovery does not mutate:

- `models.json`;
- live CTBRec;
- Mobile Reviewer state.

The user's existing 4 pending outbox rows are normal test state and must remain preserved through this patch.

## 16. Safe upgrade behavior

PATCH must not include:

- `discovery_config.json`;
- `state/discovery.sqlite3` or any state/runtime DB;
- logs;
- user local source files;
- Recu sessions/profiles/cookies;
- WM IDs;
- other mutable/private runtime state.

The v0.4.0 → v0.4.1 patch is designed to be overlaid directly on the existing installation. Database schema stays v2; no destructive migration is needed.

## 17. Validation added for v0.4.1

Current unit/regression suite: **27 tests**, including new tests for:

- nested three-bucket Mobile Reviewer catalog parsing;
- unique model aggregation across modes;
- conservative cleanup of v0.4 bucket artifacts;
- `v41:` catalog forced reparse after a prior `v4:` false success;
- `v41|` Recu local forced reparse;
- Recu match after known account import;
- collector busy isolation per source;
- lightweight `/api/auth` Reviewer health probe;
- preservation of older 401/403 auth-boundary behavior.

`validation/validate_v041.py` provides an end-to-end synthetic harness covering nested catalog, cleanup, local Live Control, local Recu, gender filtering, per-source locking, lightweight Reviewer probe, and HTTP API smoke.

Historical `validation/validate_v040.py` remains and must also pass.

## 18. Exact field test requested for v0.4.1

After overlaying PATCH onto the user's existing v0.4 installation:

1. stop Discovery;
2. overlay patch without deleting the folder;
3. restart `START_DISCOVERY.bat`;
4. wait for initial background sync to finish or click Sync once;
5. Status should show `mobile_reviewer_catalog` with **far more than 3 unique models**; detail should say `X unique models imported from Y catalog row/group records`;
6. `integration.catalog_file.catalog_unique_model_count` should be realistic for the user's library, not `3`;
7. Recu local source should re-run once even though the files did not change, because cursor generation is now `v41|`;
8. Recu detail should expose `parsed_records`, `matched_known`, `skipped_unknown`, `imported`, `per_file`;
9. click Run now on Live Control Models — it should no longer be blocked merely because Recu is running. If it says busy, the Live Control source itself is already running and Sources should show `running now`;
10. Reviewer service should normally show reachable via `GET /api/auth`; if not, the returned diagnostic should distinguish API-auth error from fallback HEAD error;
11. Discover should remain filtered to male + unknown by default and should now have real positive preference seeds if Live Control exposes favorite/priority states.

The most useful user response is the new Status JSON plus one top-of-Discover screenshot.

## 19. Next engineering priorities after v0.4.1 field validation

### P0: Discover media evidence

- index Rapid Sorter's already-local `runtime/recu_best_moments/` media/sidecars;
- Range-capable local Best Moments playback on tap;
- reuse Live Control's proven live-room preview/HLS architecture through a deliberate authenticated local broker;
- no background live stream resolution for all cards.

### P0: complete global affiliate snapshot reuse

When exact current Live Control source is available, expose a sanitized local accepted full affiliate snapshot so Discovery can retire normal direct global polling while preserving new-account discovery.

### P1: recommendation signal quality

- automated authorized/public neighbor graph ingestion;
- Recu bookmark rank/momentum/clip-velocity ingestion;
- source-ablation outcome metrics (which sources lead to Favorite/Likely Favorite/Continue);
- controlled exploration remains explicit.

### P1: session opportunity engine

Separate score/queue for unusually interesting current sessions using self-normalized anomalies. Never let raw traffic/tips inflate base affinity.

### P2: verified preference synchronization

Only after exact current bridge implementation is inspectable/testable: consume inert outbox using exact-target verified mutation path with idempotency and undo semantics.

## 20. Non-negotiable invariants

- Discovery never blocks Reviewer sorting/open paths.
- No recurring whole-library media scan.
- No direct `models.json` mutation.
- No guessed live bridge operations.
- No face recognition/biometric re-identification.
- No Recu access-control bypass or competing browser/session owner.
- No hidden high-frequency external polling.
- External failures preserve cached UI/state and fail closed.
- Media loads on demand.
- Score is explainable and not presented as probability.
- Hard eligibility filters are explicit, not learned through avoidable negative feedback.
- Every patch overlay must preserve active config/private/runtime state byte-for-byte unless an explicitly tested migration is required.

## 21. Key current files

- `discovery/importers.py` — recursive Mobile Reviewer catalog walker + v41 reparse/cleanup.
- `discovery/collectors.py` — per-source locks, local Live Control/Recu and optional collectors.
- `discovery/integration.py` — `/api/auth` Reviewer probe + corrected catalog counts.
- `discovery/store.py` — SQLite/WAL + safe v0.4 artifact cleanup helper.
- `discovery/scoring.py` — affinity scoring + global eligibility.
- `discovery/engine.py` — sync orchestration / recommendation APIs / inert outbox.
- `static/app.js` — UI incl. running-source state.
- `scripts/CHECK_CONFIG.bat` — user-safe configuration/path/port check.
- `scripts/VALIDATE.bat` — developer regression suite; not the normal user troubleshooting tool.
- `validation/validate_v041.py` — current field-adapter integration harness.

## 22. Packaging / validation status

Release validation is **GREEN**. The frozen release is gated by:

1. Python compile of all source/tests/validation: PASS;
2. 27/27 unit/regression tests: PASS;
3. Node syntax check: PASS;
4. historical `validate_v040.py`: PASS;
5. current `validate_v041.py`: PASS;
6. package safety validator: PASS;
7. v0.4.0→v0.4.1 PATCH overlay with seeded active config, SQLite DB and private runtime sentinel preserved byte-for-byte: PASS;
8. clean FULL_SOURCE extraction and full validation rerun: PASS;
9. FULL/PATCH ZIP integrity: PASS;
10. final SHA-256 checksums generated.

`TEST_REPORT_v0_4_1.txt` and `SHA256SUMS_CTBR_DISCOVERY_v0_4_1.txt` are the authoritative release records. Real-PC field validation remains required for the user's exact nested catalog and active local services.

## 23. v0.4.2 Git Bridge architecture

Primary files: `tools/git_bridge/bridge.py`, `Setup-Git-Bridge.cmd`, `Apply-ChatGPT-Update.cmd`, `Bridge-Status.cmd`.

### Repository layout

- umbrella repository: `fcd02/Chaturbate_Projects`;
- default local clone: `C:\GitHub\Chaturbate_Projects`;
- project root: `projects\ctbrec-discovery`;
- runnable public-safe source: `projects\ctbrec-discovery\app`;
- canonical repo handoff: `projects\ctbrec-discovery\HANDOFF.md`;
- versioned repo release copies: `projects\ctbrec-discovery\docs\release-notes\`;
- machine-specific bridge config: `%LOCALAPPDATA%\CTBRecDiscoveryGitBridge\config.json`;
- logs/backups/last-update receipts: `%LOCALAPPDATA%\CTBRecDiscoveryGitBridge\`.

### Setup behavior

`Setup-Git-Bridge.cmd` is run once from the current live Discovery source. It verifies Git/GitHub CLI auth, clones/reuses the umbrella repo, creates a public-safe source snapshot, updates the project index/docs, runs the full Discovery validation suite, creates a bootstrap branch/PR, squash-merges it, and creates `ctbrec-discovery-vX.Y.Z`.

The public snapshot explicitly excludes active `discovery_config.json`, `state/`, SQLite/DB files, logs, Recu/session/browser/cookie data, local Recu caches, recordings/media/model binaries, secrets/keys/token-named files, and delivery archives/encodings. `discovery_config.example.json` remains tracked.

### Future PATCH automation

Drag any future ChatGPT Discovery PATCH ZIP onto `Apply-ChatGPT-Update.cmd` (or double-click and paste a path). The bridge then:

1. rejects path traversal and protected/private/runtime payloads before extraction;
2. requires a clean Git working tree and fast-forwards `main`;
3. creates `release/ctbrec-discovery-vX.Y.Z`;
4. applies the patch only to `projects\ctbrec-discovery\app`;
5. refreshes repo `HANDOFF.md`, release notes and `PROJECTS.md`;
6. runs Python compile, the complete unittest suite, JS syntax check when Node is present, and every `validation\validate_*.py`;
7. commits and pushes the validated release;
8. opens a PR, squash-merges by default and tags the merged release;
9. backs up the exact live files touched by the patch;
10. stops the Discovery listener when running;
11. deploys the validated patch to the live runtime;
12. re-runs validation in the live runtime;
13. restarts Discovery in the background and verifies `/api/health`;
14. restores the backed-up live files if deployment/restart fails.

If PR auto-merge is disabled, live deployment is intentionally deferred so the runtime never outruns Git `main`. `--no-push` likewise skips live deployment.

### Future delivery invariant

`tools/git_bridge/` is now part of supported Discovery source. Future FULL SOURCE and PATCH packages must continue to carry the current bridge files so the bridge can update itself when its implementation changes.

## 24. v0.4.2 validation additions

`validation/validate_v042.py` tests the Git Bridge without touching GitHub: protected-path rejection, safe public snapshot filtering, version inference, project constants, example config, and bridge source/package invariants. `validation/validate_package.py` now requires the bridge files.

Release v0.4.2 does not change SQLite schema or recommendation semantics; its runtime feature delta is release/deployment tooling only. Active `discovery_config.json` and `state/` remain untouched by the v0.4.1 → v0.4.2 PATCH.

## 25. v0.4.2 frozen release validation

Frozen release gates are GREEN:

1. Python compile: PASS;
2. 27/27 unit/regression tests: PASS;
3. Node syntax check for `static/app.js`: PASS;
4. `validate_v040.py`: PASS;
5. `validate_v041.py`: PASS;
6. `validate_v042.py`: PASS;
7. `validate_package.py`: PASS after generated Python caches are removed;
8. local bare-origin Git simulation: PASS — v0.4.2 PATCH applied to a v0.4.1 repo copy, all release validation ran from the repo app, `release/ctbrec-discovery-v0.4.2` commit was created under `--no-push`, and live deployment was correctly skipped;
9. v0.4.1 → v0.4.2 overlay preservation: active `discovery_config.json`, `state/` database/sentinel and unrelated private runtime files remain byte-for-byte unchanged;
10. clean FULL SOURCE extraction rerun: PASS;
11. FULL/PATCH/standalone bridge ZIP integrity and SHA-256 generation: PASS.

`TEST_REPORT_v0_4_2.txt` and `SHA256SUMS_CTBR_DISCOVERY_v0_4_2.txt` are the authoritative release records.


## 20. v0.4.3 Git Bridge field incident and repair

### Observed field failure

On the user's Windows workstation, `Setup-Git-Bridge.cmd` successfully confirmed `gh` authentication for `fcd02`, checked out/pulled `main`, and created the public-safe Discovery baseline. Validation then stopped at `validation/validate_v040.py`. Bridge v1.0's command wrapper treated that non-zero exit as if `py` itself were unavailable and immediately reran the entire setup with `python`. The first failed run had already created untracked `.gitignore`, `PROJECTS.md`, and `projects/`, so the automatic second run failed with `Repository has uncommitted/untracked changes`.

The GitHub connector verified that remote `fcd02/Chaturbate_Projects` was not partially modified; `main` still contained only the historical `test.md`.

### Root causes

1. **Validation happened too late.** `setup()` modified the real repo working tree before running release validation.
2. **Launcher fallback logic was wrong.** `if errorlevel 1 python ...` retried on every bridge failure, not only when `py` was unavailable.
3. **A historical timing test was too brittle.** `validate_v040.py` required `App(...)` construction in `<0.5s`; this is not a useful hard threshold on a real Windows machine with filesystem/Defender overhead. Heavy collector regressions take many seconds/minutes, so a 5-second ceiling still detects the intended blocking-startup regression.
4. **Validation errors were opaque.** The bridge logged child output to disk but raised only the command name, hiding the assertion that actually failed.

### v1.0.1 behavior

- builds a temporary public-safe staging copy from the live Discovery runtime;
- runs compile/unit/JS/all validation suites against that staging copy **before** touching Git;
- only after validation passes writes the staged baseline to `projects/ctbrec-discovery/app`;
- recognizes the exact untracked marker-bearing Discovery bootstrap files left by the v1.0 failed attempt and removes only those;
- refuses to auto-clean tracked modifications or unrelated untracked user files;
- launcher scripts use `where py`/`where python` to select an interpreter once and do not retry an actual bridge failure;
- validation exceptions include the last 80 output lines;
- `validate_v040.py` defaults to a 5-second construction ceiling and allows override through `CTBREC_VALIDATION_STARTUP_MAX_SECONDS`;
- bridge config is saved only after successful bootstrap publication;
- root README/SECURITY metadata is created when absent and the old `test.md` placeholder is removed as part of the validated bootstrap commit.

### User recovery path

After applying the v0.4.3 patch over v0.4.2, rerun `tools\git_bridge\Setup-Git-Bridge.cmd`. Do **not** manually delete the local clone or run `git clean`. The bridge should print `Recovering files left by an interrupted Discovery Git Bridge setup`, return the clone to the clean pre-bootstrap state, validate in staging, and then proceed with the actual bootstrap branch/PR/tag. If unrelated user work exists in the clone, setup stops rather than deleting it.
\n\n## 26. v0.4.4 Git Bridge incremental-upgrade validation incident and repair\n\n### Observed field failure\n\nThe user's second real `Setup-Git-Bridge.cmd` attempt successfully:\n\n- authenticated to GitHub as `fcd02`;\n- recognized and safely removed the exact untracked bootstrap artifacts left by bridge v1.0;\n- returned `C:\\GitHub\\Chaturbate_Projects` to clean `main`;\n- prepared a temporary public-safe Discovery staging copy before touching the Git worktree.\n\nRelease validation then failed in that staging directory at `validation/validate_package.py` with:\n\n```text\nAssertionError: missing: ['validation/validate_v041.py']\n```\n\nThe GitHub connector again confirmed that remote `fcd02/Chaturbate_Projects` remained untouched; `main` still contained only the historical `test.md`.\n\n### Exact root cause\n\nThis was not a user configuration problem and not GitHub authentication. It was a packaging/validation contract bug.\n\n- FULL SOURCE v0.4.3 contained `validate_v040.py`, `validate_v041.py`, `validate_v042.py`, and `validate_v043.py`.\n- PATCH v0.4.3 contained only validators changed in that release (`validate_v040.py`, `validate_package.py`, `validate_v043.py`).\n- A live installation built by sequential PATCH overlays is therefore not guaranteed to contain every historical validator unless every historical patch was applied in exactly the expected chain.\n- `validate_package.py` nevertheless declared historical `validate_v041.py`, `validate_v042.py`, and `validate_v043.py` mandatory package structure.\n- The bridge correctly stages the *live runtime*; it does not secretly substitute FULL SOURCE. Therefore it exposed the invalid assumption exactly as designed.\n\n### v0.4.4 / Git Bridge v1.0.2 correction\n\nThe release invariant is now:\n\n> The current release must contain and pass the current release validator plus canonical source/tooling files. Historical validators are useful regression/archaeology assets when present, but they are not bootstrap preconditions.\n\nChanges:\n\n1. `validation/validate_package.py` requires `validation/validate_v044.py`, not v0.4.1/v0.4.2/v0.4.3 historical validators.\n2. Git Bridge release validation already discovers `validation/validate_*.py`; v1.0.2 now prints the exact inventory before running it, making missing/present validators obvious in field logs.\n3. `scripts/VALIDATE.bat` now discovers `validation\\validate_v*.py` dynamically instead of hard-coding a historical sequence.\n4. Historical `validate_v043.py` no longer pins the bridge to exactly v1.0.1; it verifies the v1.0.1 recovery guarantees remain present while allowing newer bridge versions.\n5. `validation/validate_v044.py` reproduces the user's exact failure class by building a public-safe staging copy, deleting v0.4.1/v0.4.2/v0.4.3 historical validators, and asserting the package gate still passes.\n6. The v0.4.4 PATCH intentionally includes the entire current validation directory (`validate_v040.py` through `validate_v044.py` plus `validate_package.py`) so the user's live installation self-heals immediately. Future correctness does not depend on those historical files remaining forever.\n7. Bridge version is v1.0.2; config example and bridge documentation are updated accordingly.\n\n### Safety / state invariants\n\nThis hotfix changes no SQLite schema, recommendation semantics, collector behavior, source credentials, or CTBRec mutation behavior. PATCH overlay must continue to preserve active `discovery_config.json`, `state/`, databases, logs and unrelated private runtime files byte-for-byte.\n\n### User recovery path\n\nApply v0.4.4 PATCH directly over the existing v0.4.3 live folder. Do not delete the local Git clone and do not run manual `git clean`/`git reset`. Then rerun `tools\\git_bridge\\Setup-Git-Bridge.cmd`. The staging log should print `Validation scripts present:` followed by the available validators. Missing historical validators can no longer fail the package gate.\n
## 27. v0.4.4 frozen release validation

Frozen release gates are GREEN:

1. Python compile: PASS;
2. 27/27 unit/regression tests: PASS;
3. Node syntax check: PASS;
4. `validate_v040.py`: PASS;
5. `validate_v041.py`: PASS;
6. `validate_v042.py`: PASS;
7. `validate_v043.py`: PASS after making the historical bridge-version assertion forward-compatible;
8. `validate_v044.py`: PASS, including the incremental-upgrade package-gate regression;
9. `validate_package.py`: PASS;
10. exact field-shape staging with v041/v042/v043 removed: Git Bridge `run_full_validation()` PASS with inventory `validate_package.py, validate_v040.py, validate_v044.py`;
11. exact v0.4.3 → v0.4.4 PATCH overlay against a runtime intentionally missing v041/v042: PASS; repair PATCH restored the complete current validation set;
12. seeded active `discovery_config.json`, SQLite DB and unrelated private runtime sentinel remained byte-for-byte unchanged across the exact PATCH overlay;
13. public-safe staging created from that repaired overlay ran full Git Bridge release validation with package + v040-v044 and the unit suite: PASS;
14. GitHub connector check after the user's failed v0.4.3 bootstrap: remote `fcd02/Chaturbate_Projects` remains untouched on `main` except historical `test.md`;
15. final FULL SOURCE, PATCH and standalone Git Bridge archive integrity + SHA-256 generation: required before delivery.

This release resolves the validator-history assumption at the invariant level rather than merely adding one missing file.


## 28. v0.4.5 third Git Bridge incident — root-cause redesign

### Field failure

The user's v0.4.4 bootstrap reached public-safe staging and printed the historical validator inventory, then failed in `validate_v041.py`:

```text
AssertionError: {'seen': 3, 'imported': 3, 'errors': 0}
```

This was the key evidence that v0.4.4's strategy was still structurally wrong. It had made historical validators optional *files*, but still auto-executed every historical validator that happened to exist. A validator written for v0.4.1 is not a compatibility promise for all later source.

### Permanent invariant

> Current source is validated by the current release contract only. Historical release validators are archival and must not participate in automatic pass/fail decisions for later releases.

`validation/release_manifest.json` + `validation/runner.py` are now the single source of truth. Git Bridge and `VALIDATE.bat` must never independently rediscover validators by glob.

### Required release proof

Before shipping v0.4.5, the exact final PATCH must be tested in both of these simulated prior-runtime shapes:

1. stale historical validators present (including the field-failing v0.4.1 validator);
2. historical validators removed.

Both must pass the same current-release validation. Private config/state sentinels must remain byte-for-byte unchanged. The exact final FULL SOURCE ZIP must also pass package validation after fresh extraction.

### GitHub state

After the third failure, the real GitHub repository was checked read-only again. `fcd02/Chaturbate_Projects` `main` still contained only the historical `test.md`; no partial Discovery bootstrap had been published.

## 29. Standing delivery invariant after v0.4.5

- PATCH is primary and must always be the newest artifact.
- Final PATCH is generated only after source + tests + docs + handoff freeze, then extracted/overlay-tested exactly.
- FULL SOURCE + handoff + report + checksums remain mandatory.
- No release email unless explicitly requested.
- Git/Git Bridge is the normal release-history path.


## 30. v0.4.5 frozen release validation

The authoritative-current redesign was validated against the failure mode that broke the user's first three Git Bridge bootstrap attempts. Frozen release gates are GREEN:

1. `validation/runner.py --mode package` on the complete v0.4.5 source: PASS;
2. Python compile of current runtime/tests/current validation/bridge: PASS;
3. 27/27 unit/regression tests: PASS;
4. Node syntax check for `static/app.js`: PASS;
5. `validation/validate_current.py`: PASS;
6. `validation/validate_package.py`: PASS;
7. historical-validator **absent** staging: all `validate_v0*.py` files removed, current-release package validation still PASS;
8. historical-validator **poison** staging: an extra `validate_v999.py` containing an unconditional `AssertionError` was added, current-release package validation still PASS, proving historical files are not auto-executed;
9. local bare-origin Git Bridge bootstrap simulation: PASS end-to-end through public-safe staging validation, bootstrap branch commit/push, simulated PR squash-merge, `main` refresh, `ctbrec-discovery-v0.4.5` tag, clean final worktree, and external bridge config;
10. the production GitHub repository was checked read-only before release and still contained only the historical `test.md`, confirming previous failed bootstrap attempts did not partially publish Discovery;
11. exact final PATCH ZIP overlay from v0.4.4 with active config, SQLite/private state and unrelated private sentinel preservation: PASS;
12. exact patched runtime validation with stale historical validators present: PASS;
13. exact patched runtime validation after historical validators are removed: PASS;
14. exact final FULL SOURCE ZIP fresh extraction + package validation: PASS;
15. final PATCH/FULL/standalone Git Bridge ZIP integrity and SHA-256 verification: PASS.

The important proof is not that old validators were edited until they passed. The proof is that the automatic validation path no longer depends on them at all. `release_manifest.json` selects the current contract, and both Git Bridge and `VALIDATE.bat` call only `validation/runner.py`.
