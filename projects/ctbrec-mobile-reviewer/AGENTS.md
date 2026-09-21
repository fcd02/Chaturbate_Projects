# AGENTS.md â€” CTBRec Mobile Reviewer

## Source of truth

- Runnable source: `app/`
- Current project context: `HANDOFF.md`
- Current release history: `CHANGELOG.md` plus `docs/release-notes/`
- Validation: `app/validation/`

## Required behavior for code changes

1. Preserve runtime state and previously generated trustworthy mosaics.
2. Do not commit credentials, cookies, Recu/browser sessions, recordings, generated mosaics, logs, private config, or model binaries.
3. Prefer narrow changes over broad refactors unless architecture requires otherwise.
4. Add regression coverage for every fixed bug.
5. Run Python compile checks, JavaScript syntax checks, and every validation suite before publishing.
6. Update `HANDOFF.md` and release notes with architecture changes, validation status, known issues, and exact next steps.
7. Keep `main` stable; use a release/fix branch and PR for changes.
8. Never silently discard or regenerate reusable mosaics merely because the app version changed; compatibility must be determined from actual structure/method metadata.
9. Preserve the bounded active-model look-ahead semantics and cancellation rules unless a task explicitly changes them.
10. The Git Bridge under `app/tools/git_bridge/` is part of the supported deployment workflow and must remain in future packages.
