# Release builder

`build_release.py` is the authoritative artifact builder for CTBRec Discovery.

The PATCH is intentionally **cumulative**, not a minimal diff. Every PATCH contains the complete current public runtime/source/tooling surface needed to repair a machine that skipped one or more intermediate patches. Runtime/private state is excluded.

Run it only after source, tests, current handoff, and validation docs are final. The script regenerates `validation/source_manifest.json` and `PATCH_MANIFEST.json`, then creates FULL SOURCE and PATCH ZIPs.
