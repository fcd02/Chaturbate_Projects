# CTBRec Discovery v0.4.6

## v0.4.6 — cumulative self-healing PATCH / Git Bridge v1.2.0

This release fixes the failure class behind the repeated Git Bridge bootstrap problems rather than another individual assertion.

The user's v0.4.5 staging log failed in `validate_current.py` with:

```text
AssertionError: {'seen': 3, 'imported': 3, 'errors': 0}
```

That exact `3/3` result is reproducible with the old v0.4.0 catalog importer, which treated the three outer Mobile Reviewer catalog buckets as three models. The clean v0.4.5 FULL SOURCE had the repaired importer, but the v0.4.5 PATCH was still a minimal diff and did **not** carry `discovery/importers.py`. Therefore a machine that skipped the v0.4.1 patch (or otherwise retained a stale core file) could have a v0.4.5 version marker and validation files while still executing v0.4.0 product code.

v0.4.6 permanently changes the release contract:

- **PATCH is cumulative, not a minimal diff.** It contains the complete current public source/runtime/tooling surface required to self-heal a skipped/interrupted incremental upgrade.
- `PATCH_MANIFEST.json` declares `patch_type: cumulative_public_source_overlay`, current release, minimum supported version (`0.3.0`), and source-manifest location.
- `validation/source_manifest.json` SHA-256 pins the canonical executable/validation/bridge source. Both package and runtime validation verify these hashes **before behavioral tests**. A stale `discovery/importers.py` now produces a clear source-drift error instead of a downstream `3/3` assertion.
- Git Bridge v1.2.0 rejects a future PATCH if it is not cumulative, omits any canonical source file, or contains a canonical file whose bytes do not match the source manifest.
- Catalog/Recu parser cursor generations are bumped to `v46` / `v46|` so the corrected current code reparses local data once after this repair.
- `tools/release/build_release.py` is now the authoritative artifact builder and always emits a cumulative PATCH.

## Install / recover the Git bootstrap

1. Stop Discovery.
2. Extract **`CTBRec_Discovery_v0_4_6_PATCH.zip` directly over your existing Discovery folder**, regardless of whether that folder currently identifies as v0.4.4 or v0.4.5. Do not delete the folder first.
3. Do not manually delete/reset `C:\GitHub\Chaturbate_Projects`.
4. Run `tools\git_bridge\Setup-Git-Bridge.cmd` again.
5. Early validation output should include:

```text
Validation release: 0.4.6
Canonical source manifest verified: ... files
```

If the patch was not extracted over the correct live folder, validation now stops immediately with an explicit canonical-source hash/missing-file message.

## Standing release rules

- PATCH is the primary artifact and is always regenerated **after** final source/tests/docs/handoff edits.
- Every PATCH is a cumulative public-source overlay and must support direct upgrade from the declared minimum supported version; it must never depend on every intermediate patch having been applied.
- Every build also includes FULL SOURCE, current handoff/context, validation report, and SHA-256 checksums.
- No release-email delivery; Git/Git Bridge is the project history/delivery mechanism unless explicitly requested otherwise.
- Active `discovery_config.json`, `state/`, SQLite/DBs, credentials/WM IDs, sessions/cookies, logs, local Recu caches, media, and unrelated private files never ship or get overwritten.

See `docs/RELEASE_ENGINEERING_PROMPT.md` and `CTBRec_Discovery_v0_4_6_HANDOFF.md`.

## Current product behavior retained

- Standalone service on `127.0.0.1:8793`.
- Read-only Mobile Reviewer catalog import; no whole-library scan.
- Read-only local Recu archive enrichment.
- Live Control `/api/models` loopback reuse.
- Optional direct affiliate collector remains disabled by default.
- Male recommendation eligibility enabled by default; known female/couple rooms excluded unless configured otherwise; unknown gender remains allowed.
- `Test` remains neutral for preference learning.
- Feedback/outbox remains durable but non-executing (`execute:false`); Discovery does not directly mutate `models.json`.
- No biometric face matching and no Recu access-control bypass.
