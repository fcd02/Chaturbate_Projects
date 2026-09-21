param(
    [Parameter(Position=0)]
    [string]$PatchZip = "",
    [string]$Comment = "",
    [string]$ConfigPath = "",
    [switch]$Yes,
    [switch]$NoPush,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$BridgeVersion = "1.1.0"
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-Good([string]$Message) {
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name was not found. $Hint"
    }
}

function Normalize-Path([string]$Value) {
    return [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Value).Path)
}

function Get-RelativeChildPath([string]$RootPath, [string]$FullPath) {
    $rootFull = [System.IO.Path]::GetFullPath($RootPath)
    $childFull = [System.IO.Path]::GetFullPath($FullPath)
    if (-not $childFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is not beneath expected root: $childFull"
    }
    return ($childFull.Substring($rootFull.Length) -replace '^[\\/]+','')
}

function Normalize-ComparePath([string]$Value) {
    $full = [System.IO.Path]::GetFullPath($Value)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ([string]::Equals($full, $root, [System.StringComparison]::OrdinalIgnoreCase)) { return $full }
    return ($full -replace '[\\/]+$','')
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
    if (-not $p) { return $false }
    if ($p.StartsWith('../') -or $p.Contains('/../') -or $p -match '^[a-z]:/' -or $p.StartsWith('//')) { return $true }
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

function Get-PythonCommand {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) { return [string]$pyLauncher.Source }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return [string]$python.Source }
    throw "Python was not found."
}

function Invoke-Logged([string]$Exe, [string[]]$Args, [string]$WorkingDirectory, [string]$LogPath) {
    $quoted = ($Args | ForEach-Object { if ($_ -match '\s') { '"' + $_.Replace('"','\"') + '"' } else { $_ } }) -join ' '
    "`n> $Exe $quoted" | Add-Content -LiteralPath $LogPath
    Push-Location $WorkingDirectory
    try {
        & $Exe @Args 2>&1 | Tee-Object -FilePath $LogPath -Append | Out-Host
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

function Stop-Reviewer([string]$Runtime) {
    $pidPath = Join-Path $Runtime 'mobile_server.pid'
    if (-not (Test-Path -LiteralPath $pidPath)) {
        Write-Host "Reviewer PID file not present; continuing."
        return $false
    }
    $raw = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    $pidValue = 0
    if (-not [int]::TryParse($raw, [ref]$pidValue)) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        return $false
    }
    $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if (-not $proc) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        return $false
    }
    Write-Host "Stopping Reviewer PID $pidValue..."
    & taskkill.exe /PID $pidValue /T /F | Out-Host
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 300
    }
    if (Get-Process -Id $pidValue -ErrorAction SilentlyContinue) {
        throw "Reviewer process $pidValue did not stop."
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    return $true
}

function Start-Reviewer([string]$Runtime) {
    $start = Join-Path $Runtime 'start_server_only.bat'
    if (-not (Test-Path -LiteralPath $start)) { throw "start_server_only.bat was not found in $Runtime" }
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', ('"' + $start + '"')) -WorkingDirectory $Runtime -WindowStyle Hidden | Out-Null
}

function Get-ReviewerPort([string]$Runtime) {
    $port = 8787
    $cfg = Join-Path $Runtime 'mobile_config.json'
    if (Test-Path -LiteralPath $cfg) {
        try {
            $obj = Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json
            if ($obj.port) { $port = [int]$obj.port }
        } catch { }
    }
    return $port
}

function Wait-Reviewer([string]$Runtime, [int]$Seconds = 25) {
    $port = Get-ReviewerPort $Runtime
    $uri = "http://127.0.0.1:$port/api/auth"
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

function Copy-Payload([string]$PayloadRoot, [string]$Destination) {
    Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File | ForEach-Object {
        $relative = Get-RelativeChildPath $PayloadRoot $_.FullName
        if ($relative -eq 'GIT_BRIDGE_MANIFEST.json') { return }
        $target = Join-Path $Destination $relative
        $parent = Split-Path -Parent $target
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}

function Backup-RuntimeFiles([string]$PayloadRoot, [string]$Runtime, [string]$BackupRoot) {
    $records = @()
    Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File | ForEach-Object {
        $relative = Get-RelativeChildPath $PayloadRoot $_.FullName
        if ($relative -eq 'GIT_BRIDGE_MANIFEST.json') { return }
        $target = Join-Path $Runtime $relative
        $backup = Join-Path $BackupRoot $relative
        $existed = Test-Path -LiteralPath $target
        if ($existed) {
            $parent = Split-Path -Parent $backup
            if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            Copy-Item -LiteralPath $target -Destination $backup -Force
        }
        $records += [pscustomobject]@{ relative = $relative; existed = $existed }
    }
    return $records
}

function Restore-RuntimeFiles([object[]]$Records, [string]$Runtime, [string]$BackupRoot) {
    foreach ($record in $Records) {
        $target = Join-Path $Runtime $record.relative
        $backup = Join-Path $BackupRoot $record.relative
        if ($record.existed) {
            $parent = Split-Path -Parent $target
            if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            Copy-Item -LiteralPath $backup -Destination $target -Force
        } else {
            Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        }
    }
}

function Find-PayloadRoot([string]$ExtractRoot) {
    if (Test-Path -LiteralPath (Join-Path $ExtractRoot 'ctbrec_mobile_server.py')) { return $ExtractRoot }
    $dirs = @(Get-ChildItem -LiteralPath $ExtractRoot -Directory)
    $files = @(Get-ChildItem -LiteralPath $ExtractRoot -File)
    if ($dirs.Count -eq 1 -and $files.Count -eq 0 -and (Test-Path -LiteralPath (Join-Path $dirs[0].FullName 'ctbrec_mobile_server.py'))) {
        return $dirs[0].FullName
    }
    return $ExtractRoot
}

function Infer-Version([string[]]$EntryNames) {
    foreach ($n in $EntryNames) {
        if ($n -match 'CTBRec_Mobile_Reviewer_v(\d+)_(\d+)_(\d+)_HANDOFF\.md$') {
            return "$($Matches[1]).$($Matches[2]).$($Matches[3])"
        }
    }
    foreach ($n in $EntryNames) {
        if ($n -match 'CHANGELOG_v(\d+)_(\d+)_(\d+)\.txt$') {
            return "$($Matches[1]).$($Matches[2]).$($Matches[3])"
        }
    }
    throw "Could not infer a release version from the patch. A versioned HANDOFF or CHANGELOG file is required."
}

function Refresh-RepoMetadata([string]$Repo, [string]$ProjectRoot, [string]$AppRoot, [string]$Version) {
    $token = $Version -replace '\.','_'
    $handoff = Join-Path $AppRoot "CTBRec_Mobile_Reviewer_v${token}_HANDOFF.md"
    if (Test-Path -LiteralPath $handoff) {
        Copy-Item -LiteralPath $handoff -Destination (Join-Path $ProjectRoot 'HANDOFF.md') -Force
    }
    $change = Join-Path $AppRoot "CHANGELOG_v${token}.txt"
    if (Test-Path -LiteralPath $change) {
        $releaseDir = Join-Path $ProjectRoot 'docs\release-notes'
        New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
        Copy-Item -LiteralPath $change -Destination (Join-Path $releaseDir (Split-Path -Leaf $change)) -Force
        $existing = Join-Path $ProjectRoot 'CHANGELOG.md'
        $body = Get-Content -LiteralPath $change -Raw
        $old = if (Test-Path $existing) { Get-Content -LiteralPath $existing -Raw } else { "# Changelog`r`n`r`n" }
        if ($old -notmatch [regex]::Escape("v$Version")) {
            Set-Content -LiteralPath $existing -Value ("# Changelog`r`n`r`n" + $body.Trim() + "`r`n`r`n---`r`n`r`n" + ($old -replace '^# Changelog\s*','').TrimStart()) -Encoding UTF8
        }
    }
    $projects = Join-Path $Repo 'PROJECTS.md'
    if (Test-Path -LiteralPath $projects) {
        $text = Get-Content -LiteralPath $projects -Raw
        if ($text -match '(?m)^\*\*Current stable baseline:\*\*.*$') {
            $text = [regex]::Replace($text, '(?m)^\*\*Current stable baseline:\*\*.*$', "**Current stable baseline:** v$Version")
        } else {
            $text += "`r`n**Current stable baseline:** v$Version`r`n"
        }
        Set-Content -LiteralPath $projects -Value $text -Encoding UTF8
    }
}

function Run-FullValidation([string]$AppRoot, [string]$LogPath) {
    Write-Step "Running release validation"
    $py = Get-PythonCommand
    if ((Invoke-Logged $py @('-m','compileall','-q','.') $AppRoot $LogPath) -ne 0) { return $false }

    Require-Command node "Install Node.js LTS before using the automated validation bridge."
    foreach ($js in @('static\app.js','static\rapid.js','static\service-worker.js')) {
        $full = Join-Path $AppRoot $js
        if (Test-Path -LiteralPath $full) {
            if ((Invoke-Logged 'node' @('--check',$full) $AppRoot $LogPath) -ne 0) { return $false }
        }
    }

    $tests = @(Get-ChildItem -LiteralPath (Join-Path $AppRoot 'validation') -Filter 'validate_*.py' -File | Sort-Object Name)
    $passed = 0
    foreach ($test in $tests) {
        Write-Host "Running $($test.Name)..."
        if ((Invoke-Logged $py @($test.FullName) $AppRoot $LogPath) -ne 0) {
            "FAILED: $($test.Name)" | Add-Content -LiteralPath $LogPath
            return $false
        }
        $passed++
    }
    "VALIDATION_PASS_COUNT=$passed" | Add-Content -LiteralPath $LogPath
    Write-Good "$passed/$passed validation suites passed"
    return $true
}

function Run-RuntimeSelfTest([string]$Runtime, [string]$LogPath) {
    $test = Join-Path $Runtime 'mobile_self_test.py'
    if (-not (Test-Path -LiteralPath $test)) { return $true }
    $py = Get-PythonCommand
    return (Invoke-Logged $py @($test) $Runtime $LogPath) -eq 0
}

Write-Host "CTBRec Git Bridge v$BridgeVersion update" -ForegroundColor DarkCyan

if (-not $ConfigPath) {
    $ConfigPath = Join-Path (Join-Path $env:LOCALAPPDATA 'CTBRecGitBridge') 'config.json'
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Git Bridge is not configured. Run Setup-Git-Bridge.cmd once first. Expected: $ConfigPath"
}
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json

$repo = Normalize-Path $config.repo_path
$projectRoot = Join-Path $repo $config.project_subdir
$appRoot = Join-Path $repo $config.app_subdir
$runtime = Normalize-Path $config.runtime_path
$baseBranch = if ($config.base_branch) { [string]$config.base_branch } else { 'main' }
$repoFullName = [string]$config.repository_full_name

Require-Command git "Install Git."
Require-Command gh "Install GitHub CLI and run gh auth login."

if (-not $PatchZip) {
    $PatchZip = Read-Host "Paste the path to the ChatGPT PATCH ZIP"
}
$PatchZip = $PatchZip.Trim('"')
if (-not (Test-Path -LiteralPath $PatchZip)) { throw "Patch ZIP was not found: $PatchZip" }
$PatchZip = Normalize-Path $PatchZip

Write-Step "Inspecting patch"
$zip = [System.IO.Compression.ZipFile]::OpenRead($PatchZip)
try {
    $entryNames = @($zip.Entries | Where-Object { -not $_.FullName.EndsWith('/') } | ForEach-Object { $_.FullName })
    if ($entryNames.Count -eq 0) { throw "Patch ZIP is empty." }
    $version = Infer-Version $entryNames
    $bad = @()
    foreach ($name in $entryNames) {
        if (Test-ProtectedRelativePath $name) { $bad += $name }
    }
    if ($bad.Count -gt 0) {
        throw "Patch contains protected/private/runtime paths and was rejected:`n - $($bad -join "`n - ")"
    }
} finally {
    $zip.Dispose()
}
Write-Good "Patch identified as CTBRec Mobile Reviewer v$version"

if (-not $Comment) {
    $Comment = Read-Host "Optional Git/PR comment for this release (Enter to skip)"
}
if (-not $Yes) {
    $answer = Read-Host "Validate, publish and deploy v$version? [Y/n]"
    if ($answer -and $answer -notmatch '^(?i)y(es)?$') { exit 0 }
}

$stateDir = Join-Path $env:LOCALAPPDATA 'CTBRecGitBridge'
$logDir = Join-Path $stateDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logPath = Join-Path $logDir "update-v$version-$stamp.log"
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "ctbrec-git-bridge-$([guid]::NewGuid().ToString('N'))"
$extractRoot = Join-Path $tempRoot 'patch'
$backupRoot = Join-Path $tempRoot 'runtime-backup'
New-Item -ItemType Directory -Force -Path $extractRoot,$backupRoot | Out-Null
Expand-Archive -LiteralPath $PatchZip -DestinationPath $extractRoot -Force
$payloadRoot = Find-PayloadRoot $extractRoot

$runtimeIsRepo = ([string]::Equals((Normalize-ComparePath $runtime), (Normalize-ComparePath $appRoot), [System.StringComparison]::OrdinalIgnoreCase))
$runtimeStopped = $false
$oldMain = $null
$branch = "release/ctbrec-v$version"
$prUrl = ""
$runtimeBackupRecords = @()

try {
    Ensure-GitIdentity $repo

    Write-Step "Checking Git working tree"
    & git -C $repo update-index -q --refresh
    $trackedDirty = (& git -C $repo status --porcelain --untracked-files=normal) | Where-Object { $_ -notmatch '^\?\? \.local/' }
    if ($trackedDirty) {
        throw "Repository has uncommitted/untracked changes. Commit, stash, or remove them before running the bridge:`n$($trackedDirty -join "`n")"
    }

    & git -C $repo checkout $baseBranch | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not checkout $baseBranch." }
    & git -C $repo pull --ff-only origin $baseBranch | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not fast-forward $baseBranch." }
    $oldMain = (& git -C $repo rev-parse HEAD).Trim()

    if ($runtimeIsRepo) {
        Write-Step "Runtime is the Git working tree; stopping Reviewer before changing files"
        $runtimeStopped = Stop-Reviewer $runtime
    }

    $localBranches = (& git -C $repo branch --list $branch) -join ''
    if ($localBranches.Trim()) {
        & git -C $repo branch -D $branch | Out-Host
    }
    $remoteExists = (& git -C $repo ls-remote --heads origin $branch) -join ''
    if ($remoteExists.Trim()) {
        $branch = "$branch-$stamp"
    }
    & git -C $repo checkout -b $branch | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not create branch $branch." }

    Write-Step "Applying patch to Git working copy"
    Copy-Payload $payloadRoot $appRoot
    Refresh-RepoMetadata $repo $projectRoot $appRoot $version

    if ($config.run_full_validation -ne $false) {
        if (-not (Run-FullValidation $appRoot $logPath)) {
            throw "Release validation failed. See: $logPath"
        }
    }

    Write-Step "Committing validated source"
    & git -C $repo add -A | Out-Null
    $stagedNames = (& git -C $repo diff --cached --name-only) -join "`n"
    if (-not $stagedNames.Trim()) { throw "The patch produced no Git changes." }
    $message2 = if ($Comment) { $Comment } else { "Validated automated release through CTBRec Git Bridge." }
    & git -C $repo commit -m "CTBRec Mobile Reviewer v$version" -m $message2 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "git commit failed." }

    if (-not $NoPush) {
        Write-Step "Pushing release branch"
        & git -C $repo push -u origin $branch | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "git push failed." }

        if ([string]$config.publish_mode -eq 'pull-request') {
            $token = $version -replace '\.','_'
            $changePath = Join-Path $appRoot "CHANGELOG_v${token}.txt"
            $releaseNotes = if (Test-Path $changePath) { Get-Content -LiteralPath $changePath -Raw } else { "CTBRec Mobile Reviewer v$version" }
            $passLine = (Select-String -LiteralPath $logPath -Pattern '^VALIDATION_PASS_COUNT=' -ErrorAction SilentlyContinue | Select-Object -Last 1).Line
            $passCount = if ($passLine) { $passLine.Split('=')[1] } else { 'all configured' }
            $bodyPath = Join-Path $tempRoot 'pr-body.md'
            $body = @"
## CTBRec Mobile Reviewer v$version

$Comment

### Local validation
- $passCount regression suites passed
- Python compile checks passed
- JavaScript syntax checks passed
- Patch safety filter passed

### Release notes

$releaseNotes

_Automatically prepared by CTBRec Git Bridge._
"@
            Set-Content -LiteralPath $bodyPath -Value $body -Encoding UTF8
            Write-Step "Creating GitHub pull request"
            $prOutput = & gh pr create --repo $repoFullName --base $baseBranch --head $branch --title "CTBRec Mobile Reviewer v$version" --body-file $bodyPath 2>&1
            if ($LASTEXITCODE -ne 0) { throw "gh pr create failed: $($prOutput -join ' ')" }
            $prUrl = (($prOutput | Select-Object -Last 1) -as [string]).Trim()
            Write-Good "Pull request: $prUrl"

            if ($config.auto_merge_pull_request -ne $false) {
                Write-Step "Squash-merging validated pull request"
                & gh pr merge $prUrl --repo $repoFullName --squash | Out-Host
                if ($LASTEXITCODE -ne 0) { throw "Pull request merge failed. Runtime was not updated." }
                & git -C $repo checkout $baseBranch | Out-Host
                & git -C $repo pull --ff-only origin $baseBranch | Out-Host
                if ($LASTEXITCODE -ne 0) { throw "Could not refresh merged $baseBranch." }
                & git -C $repo branch -D $branch 2>$null | Out-Null
                & git -C $repo push origin --delete $branch 2>$null | Out-Null
            }
        } else {
            throw "Unsupported publish_mode '$($config.publish_mode)'. Use pull-request."
        }

        if ($config.create_version_tag -ne $false -and $config.auto_merge_pull_request -ne $false) {
            $tag = "ctbrec-mobile-reviewer-v$version"
            $tagExists = (& git -C $repo tag --list $tag) -join ''
            if (-not $tagExists.Trim()) {
                Write-Step "Creating version tag $tag"
                & git -C $repo tag -a $tag -m "CTBRec Mobile Reviewer v$version" | Out-Null
                & git -C $repo push origin $tag | Out-Host
                if ($LASTEXITCODE -ne 0) { throw "Could not push version tag $tag." }
            }
        }
    }

    if ($NoPush) {
        Write-Host "NoPush was requested; runtime deployment is skipped so live code never outruns Git main."
        if ($runtimeIsRepo -and $runtimeStopped -and -not $NoRestart) {
            Start-Reviewer $runtime
            if (-not (Wait-Reviewer $runtime)) { throw "Reviewer did not restart after NoPush validation." }
        }
        exit 0
    }

    if ($config.auto_merge_pull_request -eq $false) {
        Write-Host "Pull request was created but not merged. Runtime deployment is intentionally deferred until main contains the release." -ForegroundColor Yellow
        if ($runtimeIsRepo -and $runtimeStopped -and -not $NoRestart) {
            & git -C $repo checkout $baseBranch | Out-Host
            & git -C $repo reset --hard origin/$baseBranch | Out-Host
            Start-Reviewer $runtime
            if (-not (Wait-Reviewer $runtime)) { throw "Reviewer did not restart after deferred deployment." }
        }
        exit 0
    }

    if (-not $runtimeIsRepo) {
        Write-Step "Stopping current live Reviewer"
        $runtimeStopped = Stop-Reviewer $runtime
        Write-Step "Backing up live files touched by this patch"
        $runtimeBackupRecords = @(Backup-RuntimeFiles $payloadRoot $runtime $backupRoot)
        Write-Step "Deploying validated patch to live Reviewer"
        Copy-Payload $payloadRoot $runtime
    }

    if (-not (Run-RuntimeSelfTest $runtime $logPath)) {
        throw "Live mobile_self_test.py failed after deployment."
    }

    if ($NoRestart -or $config.restart_after_update -eq $false) {
        Write-Host "Update deployed. Restart was disabled." -ForegroundColor Yellow
    } else {
        Write-Step "Starting updated Reviewer"
        Start-Reviewer $runtime
        if (-not (Wait-Reviewer $runtime)) {
            throw "Updated Reviewer did not answer on its local /api/auth endpoint."
        }
        Write-Good "Updated Reviewer is running and responding locally"
    }

    $result = [ordered]@{
        version = $version
        completed_at = (Get-Date).ToString('o')
        repository = $repoFullName
        branch = $branch
        pull_request = $prUrl
        runtime_path = $runtime
        log = $logPath
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stateDir 'last-update.json') -Encoding UTF8

    Write-Host "`nCTBRec Mobile Reviewer v$version published and deployed successfully." -ForegroundColor Green
    Write-Host "Validation log: $logPath"
    if ($prUrl) { Write-Host "Pull request:    $prUrl" }
}
catch {
    $failure = $_.Exception.Message
    Write-Host "`nUPDATE FAILED: $failure" -ForegroundColor Red
    "UPDATE FAILED: $failure`n$($_ | Out-String)" | Add-Content -LiteralPath $logPath -ErrorAction SilentlyContinue

    if ($runtimeBackupRecords.Count -gt 0) {
        try {
            Write-Host "Restoring previous live files..." -ForegroundColor Yellow
            Stop-Reviewer $runtime | Out-Null
            Restore-RuntimeFiles $runtimeBackupRecords $runtime $backupRoot
            if (-not $NoRestart) {
                Start-Reviewer $runtime
                if (Wait-Reviewer $runtime) { Write-Good "Previous Reviewer version restored and restarted" }
                else { Write-Host "Rollback files were restored, but the previous server did not answer. Check $logPath" -ForegroundColor Red }
            }
        } catch {
            Write-Host "Rollback encountered an error: $($_.Exception.Message)" -ForegroundColor Red
        }
    } elseif ($runtimeIsRepo -and $oldMain) {
        try {
            Write-Host "Restoring Git working tree to previous main commit..." -ForegroundColor Yellow
            & git -C $repo checkout $baseBranch | Out-Null
            & git -C $repo reset --hard $oldMain | Out-Null
            if ($runtimeStopped -and -not $NoRestart) {
                Start-Reviewer $runtime
                if (Wait-Reviewer $runtime) { Write-Good "Previous Reviewer version restored and restarted" }
            }
        } catch { }
    }

    try {
        & git -C $repo checkout $baseBranch | Out-Null
        if ($oldMain) { & git -C $repo reset --hard origin/$baseBranch | Out-Null }
        $branchExists = (& git -C $repo branch --list $branch) -join ''
        if ($branchExists.Trim()) { & git -C $repo branch -D $branch | Out-Null }
    } catch { }

    Write-Host "Log: $logPath"
    exit 1
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
