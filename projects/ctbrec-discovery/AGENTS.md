# AGENTS.md — CTBRec Discovery

## Source of truth

- Runnable source: `app/`
- Current handoff: `HANDOFF.md`
- Regression tests: `app/tests/` + `app/validation/`

## Required behavior

1. Never commit `discovery_config.json`, state databases, logs, cookies/sessions, WM IDs, local recordings/media, or machine-specific secrets.
2. Preserve the three-engine separation: model affinity, account continuity, session opportunity.
3. Discovery must never block the Mobile Reviewer hot path or create a recurring whole-library scan.
4. Prefer reuse of Live Control / Mobile Reviewer / Rapid Sorter state over duplicate polling or authentication.
5. No direct `models.json` mutation and no guessed live-bridge operations.
6. Keep external collection conservative, explainable and deduplicated.
7. Add regression coverage for bugs and run all validation before publishing.
8. Every release updates the versioned handoff and keeps `CHATGPT_HANDOFF.md` current.
9. `app/tools/git_bridge/` is part of the supported release workflow and must remain in future FULL SOURCE and PATCH packages.
10. Keep `main` stable; publish through a release branch/PR unless explicitly configured otherwise.
