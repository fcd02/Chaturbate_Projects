# CTBRec Git Bridge v1.1.0

Windows field hotfix: v1.1.0 replaces the invalid PowerShell `TrimStart('\\','/')`/`TrimEnd('\\')` usage with separator-safe regex path handling, fixes scalar Python executable resolution, and configures a repository-local Git identity automatically when needed. If v1.0 failed during **Copying public-safe Reviewer source**, install the current Reviewer patch and simply rerun `Setup-Git-Bridge.cmd`; the failed run did not publish a partial baseline.

# CTBRec Git Bridge

This tool keeps the normal ChatGPT workflow while automating the mechanical deployment work that previously required manual ZIP copying.

## What it does

### One-time setup
Run `Setup-Git-Bridge.cmd` from your current CTBRec Mobile Reviewer folder.

It will:

1. verify `git` and GitHub CLI (`gh`);
2. clone `fcd02/Chaturbate_Projects` to `C:\GitHub\Chaturbate_Projects` if needed;
3. create the umbrella multi-project structure;
4. copy a **public-safe** snapshot of the current Reviewer source into `projects\ctbrec-mobile-reviewer\app`;
5. create current `HANDOFF.md`, project docs and repository safety rules;
6. save local bridge configuration outside Git under `%LOCALAPPDATA%\CTBRecGitBridge\config.json`;
7. commit and push the baseline.

The currently-running Reviewer can remain in its existing folder. The config remembers that runtime path.

### Future ChatGPT updates
Download the PATCH ZIP I give you, then either:

- drag it onto `Apply-ChatGPT-Update.cmd`, or
- double-click `Apply-ChatGPT-Update.cmd` and paste/browse the patch path when prompted.

The bridge then:

1. checks that the Git working tree is clean and fast-forwards `main`;
2. validates the patch contents and rejects credentials/runtime state/media/path traversal;
3. creates a release branch;
4. applies the patch to the Git copy;
5. refreshes canonical project `HANDOFF.md`, release notes and root version index;
6. runs Python compile checks, JavaScript syntax checks and every `validation\validate_*.py` test;
7. if validation fails, resets the Git copy and **does not touch the live Reviewer**;
8. commits with the release version and your optional comment;
9. pushes the branch and creates a GitHub pull request containing release notes + validation summary;
10. when configured, merges the PR and creates a version tag;
11. stops the currently-running Reviewer using `mobile_server.pid`;
12. backs up every live file the patch is about to replace;
13. applies the validated patch to the live installation;
14. runs `mobile_self_test.py` there;
15. starts the updated server and verifies the local HTTP endpoint responds;
16. if deployment/startup fails, restores the previous live files and restarts the prior version.

## Safety model

The bridge refuses patches containing common private/runtime material, including:

- `mobile_config.json`
- `recording_roots.txt`
- Recu cookies/session/browser profiles
- READY/catalog/library/action-queue state
- hidden-model/runtime caches
- logs
- recordings and generated media
- ONNX/model binaries
- `.env`, credentials, keys and secret files

That means the public GitHub repository can contain essentially the complete runnable source without exposing reusable credentials or your recording library.

## Where configuration lives

Local configuration is written to:

`%LOCALAPPDATA%\CTBRecGitBridge\config.json`

It is intentionally outside the repository so local paths are never committed.

Logs and rollback metadata live under:

`%LOCALAPPDATA%\CTBRecGitBridge\logs\`

## Git strategy

Default workflow:

`main -> release/ctbrec-vX.Y.Z -> local validation -> push -> PR -> squash merge -> tag`

This gives the public project useful history and release comments without making you manually perform Git operations.


## Codex CLI helper

If PowerShell blocks `npm.ps1`, run `Install-Codex-CLI.cmd`. It deliberately calls `npm.cmd` instead of the blocked PowerShell shim, installs/updates `@openai/codex`, and verifies `codex.cmd`.

## Updating the bridge itself

The Git Bridge lives inside the Reviewer source under `tools\git_bridge\`. Future PATCH/FULL SOURCE packages include it, so the bridge can update itself along with the project.
