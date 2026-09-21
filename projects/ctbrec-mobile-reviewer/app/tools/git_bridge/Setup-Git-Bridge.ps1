param(
    [string]$Repository = "fcd02/Chaturbate_Projects",
    [string]$RepoPath = "C:\GitHub\Chaturbate_Projects",
    [string]$RuntimePath = "",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$BridgeVersion = "1.1.0"

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name was not found. $Hint"
    }
}

function Normalize-FullPath([string]$PathValue) {
    return [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $PathValue).Path)
}

function Get-RelativeChildPath([string]$RootPath, [string]$FullPath) {
    $rootFull = [System.IO.Path]::GetFullPath($RootPath)
    $childFull = [System.IO.Path]::GetFullPath($FullPath)
    if (-not $childFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is not beneath expected root: $childFull"
    }
    # PowerShell does not use backslash as a string escape.  Avoid TrimStart('\\')
    # entirely so Windows PowerShell 5.1 cannot bind a two-character string to System.Char.
    return ($childFull.Substring($rootFull.Length) -replace '^[\\/]+','')
}

function Ensure-GitIdentity([string]$RepositoryPath) {
    $name = ((& git -C $RepositoryPath config --get user.name 2>$null) -join '').Trim()
    $email = ((& git -C $RepositoryPath config --get user.email 2>$null) -join '').Trim()
    if ($name -and $email) { return }

    $login = ((& gh api user --jq '.login' 2>$null) -join '').Trim()
    $id = ((& gh api user --jq '.id' 2>$null) -join '').Trim()
    if (-not $login) { $login = 'ctbrec-user' }
    if (-not $name) { & git -C $RepositoryPath config user.name $login | Out-Null }
    if (-not $email) {
        $fallbackEmail = if ($id -and $login) { "$id+$login@users.noreply.github.com" } else { 'ctbrec-user@users.noreply.github.com' }
        & git -C $RepositoryPath config user.email $fallbackEmail | Out-Null
    }
}

function Test-ProtectedRelativePath([string]$RelativePath) {
    $p = ($RelativePath -replace '\\','/').TrimStart('/').ToLowerInvariant()
    if ($p -match '(^|/)\.env($|\.)') { return $true }
    if ($p -match '(^|/)(\.git|recu_browser_profile|mobile_delete_mosaics|diagnostics|tmp|temp)(/|$)') { return $true }
    if ($p -match '(^|/)models(/|$)' -and $p -notmatch '(^|/)models/readme\.md$') { return $true }
    if ($p -match '(secret|credential|cookie|session-token|auth-token|api[_-]?key)') { return $true }
    if ($p -match '\.(pem|key|p12|pfx|onnx|ts|mp4|mkv|webm|mov|avi)$') { return $true }
    if ($p -match '\.log$') { return $true }

    $blocked = @(
        'mobile_config.json','recording_roots.txt','keeplasts.txt','mobile_recu_session.json',
        'mobile_action_queue.json','mobile_catalog_cache.json','mobile_ready_work_index.json',
        'mobile_offline_sync_history.json','mobile_library_mosaic_state.json','mobile_library_mosaic_plan.json',
        'mobile_non_nsfw_index.json','mobile_non_nsfw_state.json','mobile_mosaic_layout_cache.json',
        'mobile_hidden_models.json','mobile_model_metadata.json','mobile_server.pid',
        'mosaic_lite_duration_cache.json','mosaic_lite_model_names.json','mosaic_manifest.json',
        'mosaic_lite_recu_cache.json','mosaic_lite_transcript_cache.json','review_sort_duration_cache.json',
        'review_model_size_cache.json','review_mosaic_manifest.json','git_bridge.local.json'
    )
    $name = [System.IO.Path]::GetFileName($p)
    return $blocked -contains $name
}

function Copy-PublicSource([string]$Source, [string]$Destination) {
    Write-Step "Copying public-safe Reviewer source into the repository"
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null

    Get-ChildItem -LiteralPath $Source -Recurse -File | ForEach-Object {
        $relative = Get-RelativeChildPath $Source $_.FullName
        if (Test-ProtectedRelativePath $relative) { return }
        if ($relative -match '(^|[\\/])__pycache__([\\/]|$)') { return }
        if ($relative -match '\.py[co]$') { return }

        $target = Join-Path $Destination $relative
        $parent = Split-Path -Parent $target
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}

function Write-IfMissing([string]$PathValue, [string]$Content) {
    if (-not (Test-Path -LiteralPath $PathValue)) {
        $parent = Split-Path -Parent $PathValue
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Set-Content -LiteralPath $PathValue -Value $Content -Encoding UTF8
    }
}

Write-Host "CTBRec Git Bridge v$BridgeVersion setup" -ForegroundColor DarkCyan

Require-Command git "Install Git first (winget install --id Git.Git -e)."
Require-Command gh "Install GitHub CLI first (winget install --id GitHub.cli -e)."

if (-not $RuntimePath) {
    $RuntimePath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
}
if (-not (Test-Path -LiteralPath (Join-Path $RuntimePath "ctbrec_mobile_server.py"))) {
    throw "RuntimePath does not look like a CTBRec Mobile Reviewer folder: $RuntimePath"
}
$RuntimePath = Normalize-FullPath $RuntimePath

Write-Step "Checking GitHub authentication"
& gh auth status 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "GitHub CLI is not authenticated. Run: gh auth login" }

if (-not (Test-Path -LiteralPath $RepoPath)) {
    Write-Step "Cloning $Repository"
    $parent = Split-Path -Parent $RepoPath
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    & gh repo clone $Repository $RepoPath
    if ($LASTEXITCODE -ne 0) { throw "gh repo clone failed." }
} elseif (-not (Test-Path -LiteralPath (Join-Path $RepoPath ".git"))) {
    throw "$RepoPath exists but is not a Git repository. Choose a different RepoPath or remove that folder."
}
$RepoPath = Normalize-FullPath $RepoPath

Write-Step "Fast-forwarding repository main branch"
& git -C $RepoPath checkout main | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Could not checkout main." }
& git -C $RepoPath pull --ff-only origin main | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Could not fast-forward main." }

$projectRoot = Join-Path $RepoPath "projects\ctbrec-mobile-reviewer"
$appPath = Join-Path $projectRoot "app"

$rootReadme = @'
# Chaturbate Projects

Umbrella repository for independent Chaturbate/CTBRec-related software projects.

Each project lives under `projects/<project-name>/` and carries its own runnable source, documentation, validation, release history, and current human/AI handoff context.

See [PROJECTS.md](PROJECTS.md) for the active project index.

## Public repository policy

This repository intentionally contains source code and documentation that are safe to publish. Runtime state, recordings, generated mosaics, logs, cookies, Recu/browser sessions, credentials, machine-specific private configuration, and model binaries are excluded.
'@

$projectsIndex = @'
# Project Index

## CTBRec Mobile Reviewer / VaultFlow

**Path:** `projects/ctbrec-mobile-reviewer/`  
**Current stable baseline:** managed by the Git Bridge from the latest validated release.

This project contains the mobile/desktop CTBRec recording review and sorting workflow, including mosaics, Rapid Sort, preview/playback, Keep Last, Recu integration, and related tooling.

Future independent projects should be added as sibling folders under `projects/`.
'@

$rootSecurity = @'
# Security and private-data policy

Do not commit reusable credentials or private runtime state.

Never commit passwords, auth tokens, cookies, browser/session profiles, API keys, private recordings, generated mosaics, local logs containing sensitive data, or machine-specific secret configuration.

Use sanitized templates and environment/local configuration outside Git. If a credential is ever committed, rotate it immediately before cleaning repository history.
'@

$rootIgnore = @'
# Local/private workspace
.local/
.env
.env.*
!.env.example

# Python / editors
**/__pycache__/
**/*.py[cod]
.vscode/
.idea/
.DS_Store
Thumbs.db

# CTBRec private/runtime state
**/mobile_config.json
**/recording_roots.txt
**/keeplasts.txt
**/mobile_recu_session.json
**/mobile_action_queue.json
**/mobile_catalog_cache.json
**/mobile_ready_work_index.json
**/mobile_offline_sync_history.json
**/mobile_library_mosaic_state.json
**/mobile_library_mosaic_plan.json
**/mobile_non_nsfw_index.json
**/mobile_non_nsfw_state.json
**/mobile_mosaic_layout_cache.json
**/mobile_hidden_models.json
**/mobile_model_metadata.json
**/mobile_server.pid
**/mobile_reviewer.log
**/*duration_cache*.json
**/*transcript_cache*.json
**/*model_size_cache*.json
**/*manifest.json
**/recu_browser_profile/
**/mobile_delete_mosaics/
**/diagnostics/

# Media / generated assets / model binaries
**/models/**
!**/models/README.md
**/*.onnx
**/*.ts
**/*.mp4
**/*.mkv
**/*.webm
**/*.mov
**/*.avi
**/*mosaic*.jpg
**/*mosaic*.jpeg
**/*mosaic*.png

# Build/release archives
**/*.zip
**/*.7z
**/*.rar
'@

$projectReadme = @'
# CTBRec Mobile Reviewer / VaultFlow

The exact runnable source is kept under [`app/`](app/).

- `HANDOFF.md` is the canonical current continuation context for a human or AI agent.
- `docs/release-notes/` stores version-specific release notes copied from validated packages.
- `app/validation/` contains the executable regression suite.
- runtime state and credentials are intentionally not tracked in Git.

Any substantive release should update source, validation, release notes, and `HANDOFF.md` together.
'@

$agents = @'
# AGENTS.md — CTBRec Mobile Reviewer

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
'@

Write-IfMissing (Join-Path $RepoPath "README.md") $rootReadme
Write-IfMissing (Join-Path $RepoPath "PROJECTS.md") $projectsIndex
Write-IfMissing (Join-Path $RepoPath "SECURITY.md") $rootSecurity
Write-IfMissing (Join-Path $RepoPath ".gitignore") $rootIgnore
Write-IfMissing (Join-Path $projectRoot "README.md") $projectReadme
Write-IfMissing (Join-Path $projectRoot "AGENTS.md") $agents

Copy-PublicSource $RuntimePath $appPath

$handoffCandidates = @(Get-ChildItem -LiteralPath $RuntimePath -Filter "CTBRec_Mobile_Reviewer_v*_HANDOFF.md" -File | ForEach-Object {
    if ($_.Name -match '^CTBRec_Mobile_Reviewer_v(\d+)_(\d+)_(\d+)_HANDOFF\.md$') {
        [pscustomobject]@{ File = $_; Version = [version]("$($Matches[1]).$($Matches[2]).$($Matches[3])") }
    }
})
$handoff = $handoffCandidates | Sort-Object Version -Descending | Select-Object -First 1
if ($handoff) {
    Copy-Item -LiteralPath $handoff.File.FullName -Destination (Join-Path $projectRoot "HANDOFF.md") -Force
}

$releaseDir = Join-Path $projectRoot "docs\release-notes"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
$changeCandidates = @(Get-ChildItem -LiteralPath $RuntimePath -Filter "CHANGELOG_v*.txt" -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $releaseDir $_.Name) -Force
    if ($_.Name -match '^CHANGELOG_v(\d+)_(\d+)_(\d+)\.txt$') {
        [pscustomobject]@{ File = $_; Version = [version]("$($Matches[1]).$($Matches[2]).$($Matches[3])") }
    }
})

$canonicalChange = Join-Path $projectRoot "CHANGELOG.md"
if (-not (Test-Path $canonicalChange)) {
    $latestChange = $changeCandidates | Sort-Object Version -Descending | Select-Object -First 1
    if ($latestChange) { Copy-Item $latestChange.File.FullName $canonicalChange -Force }
    else { Set-Content $canonicalChange "# Changelog`r`n" -Encoding UTF8 }
}

$configDir = Join-Path $env:LOCALAPPDATA "CTBRecGitBridge"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$configPath = Join-Path $configDir "config.json"
$config = [ordered]@{
    repository_full_name = $Repository
    repo_path = $RepoPath
    project_subdir = "projects\ctbrec-mobile-reviewer"
    app_subdir = "projects\ctbrec-mobile-reviewer\app"
    runtime_path = $RuntimePath
    base_branch = "main"
    publish_mode = "pull-request"
    auto_merge_pull_request = $true
    create_version_tag = $true
    run_full_validation = $true
    restart_after_update = $true
}
$config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $configPath -Encoding UTF8

if (Test-Path (Join-Path $RepoPath "test.md")) {
    Remove-Item -LiteralPath (Join-Path $RepoPath "test.md") -Force
}

Ensure-GitIdentity $RepoPath

Write-Step "Reviewing files that will be committed"
& git -C $RepoPath status --short | Out-Host

if (-not $Yes) {
    $answer = Read-Host "Commit and push this sanitized baseline to GitHub? [Y/n]"
    if ($answer -and $answer -notmatch '^(?i)y(es)?$') {
        Write-Host "Setup files remain locally. Nothing was pushed."
        exit 0
    }
}

& git -C $RepoPath add -A
if ($LASTEXITCODE -ne 0) { throw "git add failed." }
$staged = (& git -C $RepoPath diff --cached --name-only) -join "`n"
if (-not $staged.Trim()) {
    Write-Host "Repository already matches this baseline; no commit needed."
} else {
    & git -C $RepoPath commit -m "Initialize CTBRec Mobile Reviewer baseline" -m "Add sanitized runnable source, current handoff, regression coverage, repository safety rules, and Git Bridge deployment tooling."
    if ($LASTEXITCODE -ne 0) { throw "git commit failed." }
    & git -C $RepoPath push origin main
    if ($LASTEXITCODE -ne 0) { throw "git push failed." }
}

Write-Host "`nGit Bridge setup complete." -ForegroundColor Green
Write-Host "Repository: $RepoPath"
Write-Host "Runtime:    $RuntimePath"
Write-Host "Config:     $configPath"
Write-Host "`nFor future releases, drag a ChatGPT PATCH ZIP onto Apply-ChatGPT-Update.cmd."
