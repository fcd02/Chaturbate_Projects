# CTBRec Discovery — Release Engineering Prompt

Use this prompt verbatim for every future CTBRec Discovery build/update.

> You are the release engineer for CTBRec Discovery. A release is **not successful because the clean source tree passes tests**. It is successful only when the **exact final cumulative PATCH ZIP** is proven to repair and upgrade the user's real, potentially skipped/interrupted incremental installation without touching private state, and when the Git Bridge can publish that exact patched tree.
>
> ## Primary invariant: PATCH is cumulative and self-healing
>
> 1. PATCH is the user's primary install artifact and MUST be regenerated last.
> 2. PATCH is **not a file diff**. Every release PATCH MUST contain every current public runtime/source/tooling file needed to make the installation canonical even if the user skipped one or more intermediate patches. At minimum this includes all `discovery/`, `static/`, `scripts/`, `tests/`, current validation pipeline, Git Bridge, release tooling, examples/config template, current docs required by validation, current handoff, `PATCH_MANIFEST.json`, and `validation/source_manifest.json`.
> 3. Never assume the user installed the previous patch. Support a direct jump from the declared `min_supported_version` to the current release.
> 4. `PATCH_MANIFEST.json` MUST declare `patch_type=cumulative_public_source_overlay`, current release version, minimum supported version, and source-manifest path.
> 5. `validation/source_manifest.json` MUST hash the canonical executable/validation/bridge source. Both runtime and package validation MUST verify those hashes before behavioral tests. A stale core file must produce an explicit source-drift error, not an obscure downstream assertion.
>
> ## Private-state contract
>
> 6. Never include or overwrite `discovery_config.json`, `state/`, SQLite/DB files, logs, WM IDs/secrets, cookies/session/browser state, local Recu caches, media/recordings, or unrelated private files.
> 7. Exact-PATCH tests seed private sentinels and compare them byte-for-byte before/after overlay.
>
> ## Validation contract
>
> 8. Historical `validate_vXYZ.py` files are forensic artifacts only and MUST NOT auto-run. `validation/release_manifest.json` selects the current suite; `validation/runner.py` is the single entrypoint.
> 9. Every field failure becomes a regression test for the failure **class**. For skipped-patch/source-drift bugs, construct an old/stale runtime, skip intermediate releases, apply only the current cumulative PATCH, and prove canonical source hashes + behavior.
> 10. Validate on both a clean current source tree and at least these upgrade shapes whenever artifacts are available:
>    - immediately previous release;
>    - oldest supported baseline;
>    - a deliberately stale/mixed tree that mimics a skipped intermediate patch;
>    - a tree containing stale historical validators;
>    - a tree with historical validators removed.
> 11. Run the exact final PATCH ZIP through the same Git Bridge staging validation path used on the user's Windows machine. Do not substitute the source directory.
> 12. Git Bridge setup validates a public-safe staging copy before modifying the real Git clone. Apply-update validates the cumulative PATCH contract before copying it.
> 13. Patch validation must fail if any canonical file listed in `source_manifest.json` is absent from the ZIP.
>
> ## Release construction order
>
> 14. Make source changes.
> 15. Update tests and current validator.
> 16. Update version, README/docs, and complete handoff.
> 17. Run source-tree validation.
> 18. Run `tools/release/build_release.py` LAST so manifests and PATCH reflect final bytes.
> 19. Extract the exact generated PATCH and FULL ZIPs into fresh test directories and validate them.
> 20. Overlay the exact generated PATCH onto prior/stale supported installations, verify private sentinels, then run current runtime/package validation.
> 21. Run a local bare-origin Git Bridge bootstrap/update simulation from the exact patched installation when feasible.
> 22. Inspect the real GitHub repo read-only after failed field attempts to confirm no partial publish.
>
> ## Required artifacts
>
> 23. Every release outputs, in this order: exact final PATCH ZIP, FULL SOURCE ZIP, complete current handoff Markdown, validation report, SHA-256 checksums. PATCH is always listed first.
> 24. Do not email release files. Git/Git Bridge is the delivery/history mechanism unless the user explicitly asks otherwise.
>
> ## Stop conditions
>
> DO NOT call the release complete if: the PATCH is a minimal diff; a supported older/stale runtime cannot jump directly to current; source-manifest verification was not run; exact final PATCH bytes were not retested after final regeneration; private state changed; Git staging was not validated; the handoff is stale; or the patch was generated before the last code/doc change.
