# CTBRec Discovery Git Bridge

This is the Discovery counterpart to the existing CTBRec Mobile Reviewer Git Bridge. It keeps the same umbrella repository and gives Discovery its own isolated project folder:

`C:\GitHub\Chaturbate_Projects\projects\ctbrec-discovery\app`

Default GitHub repository: `fcd02/Chaturbate_Projects`.


## v1.2.0 authoritative current-release validation

v1.2.0 replaces the brittle historical-validator execution model. The bridge now calls `validation/runner.py`, which reads `validation/release_manifest.json` and executes only the validator(s) explicitly declared authoritative for the current release. Historical `validate_vXYZ.py` files may be present, missing, or incompatible with later source and are never auto-executed. This directly fixes the Windows bootstrap failure where stale `validate_v041.py` rejected valid current catalog behavior. `scripts\VALIDATE.bat` uses the same runner, so local validation and Git Bridge validation cannot diverge.

## v1.0.2 upgrade-path validation hotfix

v1.0.2 fixed the first incremental-upgrade package-gate problem, but its strategy of executing every historical validator that happened to be present was still wrong. v1.2.0 supersedes that behavior with the explicit current-release manifest described above.

## v1.0.1 field hotfix

The first Windows bootstrap field test exposed three issues in v1.0:

- setup wrote repo files before validation, so a failed validation left the clone dirty;
- the `.cmd` wrappers retried the whole bridge with `python` after *any* nonzero `py -3.10` exit, causing a second misleading dirty-tree failure;
- the historical v0.4 validation required app construction in under 0.5 seconds, which is unnecessarily flaky on real Windows/Defender machines.

v1.0.1 validates a temporary public-safe staging copy first, automatically removes only unmistakable untracked Discovery bootstrap artifacts from a prior failed v1.0 run, surfaces the failing validation output tail, never retries a failed bridge command under a second interpreter, and keeps a still-strict but workstation-tolerant 5-second startup gate. Tracked or unrelated local Git work is never auto-cleaned.

## One-time setup

From the **current live CTBRec Discovery folder**, run:

`tools\git_bridge\Setup-Git-Bridge.cmd`

The bridge will:

1. verify Git and GitHub CLI;
2. verify `gh auth status`;
3. reuse/clone `fcd02/Chaturbate_Projects` at `C:\GitHub\Chaturbate_Projects`;
4. create/update `projects\ctbrec-discovery\app` with a public-safe snapshot of the current Discovery source;
5. explicitly exclude active `discovery_config.json`, SQLite/state, logs, cookies/sessions, local Recu archives, recordings/media, credentials and WM IDs;
6. create/update project `HANDOFF.md`, `AGENTS.md`, project README and the umbrella `PROJECTS.md` index;
7. run the complete Discovery validation suite;
8. save machine-specific bridge config outside Git at `%LOCALAPPDATA%\CTBRecDiscoveryGitBridge\config.json`;
9. commit to a bootstrap branch, push, open a PR, squash-merge it, and create a `ctbrec-discovery-vX.Y.Z` tag.

The live Discovery installation stays where it is. The repo copy is separate and safe to publish.

## Future ChatGPT PATCH releases

For every future Discovery release, download the PATCH ZIP and either:

- drag the PATCH ZIP onto `Apply-ChatGPT-Update.cmd`, or
- double-click `Apply-ChatGPT-Update.cmd` and paste the PATCH path.

The bridge then automatically:

1. rejects path traversal and private/runtime/credential files before extraction;
2. fast-forwards umbrella repo `main` and requires a clean Git working tree;
3. creates `release/ctbrec-discovery-vX.Y.Z`;
4. applies the patch to `projects\ctbrec-discovery\app`;
5. refreshes the canonical repo handoff/release notes/project index;
6. runs the authoritative current-release validation pipeline declared by `validation\release_manifest.json`;
7. commits the validated release;
8. pushes it and creates a GitHub pull request;
9. by default squash-merges the PR and creates a version tag;
10. backs up the live files touched by the patch;
11. stops the current Discovery service if it is listening on its configured port;
12. applies the already-validated patch to the live installation;
13. re-runs validation in the live folder;
14. restarts Discovery in the background and verifies `/api/health`;
15. rolls back the live touched files if deployment or restart fails.

## Private-state safety

The bridge will not copy/commit or accept a patch containing common private/runtime material, including:

- `discovery_config.json` (contains local paths and can contain a Chaturbate WM ID);
- `state/`, SQLite/DB files;
- logs;
- Recu browser/session/cookie state;
- local Recu archives/caches;
- media/recordings/model binaries;
- `.env`, keys, credentials, token-named files;
- ZIP/7z/RAR or Base64/Base85 delivery artifacts.

`discovery_config.example.json` remains safe and is tracked.

## Configuration

Machine-specific bridge configuration lives at:

`%LOCALAPPDATA%\CTBRecDiscoveryGitBridge\config.json`

Logs, rollback backups and the last-update receipt live under the same directory. This is deliberately outside Git.

## Git strategy

Default:

`main -> release/ctbrec-discovery-vX.Y.Z -> validation -> push -> PR -> squash merge -> tag -> live deploy`

This keeps `main` stable and creates a reviewable release history while avoiding manual Git operations for normal ChatGPT patches.

## Status helper

Run `Bridge-Status.cmd` to display the configured repo/runtime paths, current runtime version and local `/api/health` reachability.

## Important future-release requirement

`tools\git_bridge\` is part of the CTBRec Discovery supported source. Future FULL SOURCE and PATCH packages must continue to carry the current bridge files so the bridge can update itself along with the project when necessary.
