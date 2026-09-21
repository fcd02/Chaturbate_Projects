# Codex CLI on this Windows PC

The common PowerShell error:

`npm.ps1 cannot be loaded because running scripts is disabled on this system`

means Node/npm installed successfully, but PowerShell selected npm's `.ps1` wrapper and the current execution policy blocks PowerShell scripts. It does **not** mean Codex is incompatible with the computer.

The simplest fix is to call the Windows command shim instead:

```powershell
npm.cmd install -g @openai/codex@latest
```

Then close/reopen PowerShell and run:

```powershell
codex --version
codex --login
```

If `codex` is still not found in the current window:

```powershell
$env:Path += ";$env:APPDATA\npm"
& "$env:APPDATA\npm\codex.cmd" --version
```

An optional persistent PowerShell policy change for the current Windows user is:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

You do not need to change the policy to use the Git Bridge; its `.cmd` launchers invoke PowerShell with a process-local `-ExecutionPolicy Bypass`.

## Clone the repository

GitHub CLI authentication is already successful when `gh auth login` says `Logged in as fcd02`.

The earlier `cd C:\GitHub\Chaturbate_Projects` error simply means that repository had not been cloned yet.

```powershell
New-Item -ItemType Directory -Force C:\GitHub | Out-Null
gh repo clone fcd02/Chaturbate_Projects C:\GitHub\Chaturbate_Projects
cd C:\GitHub\Chaturbate_Projects
```

After the Git Bridge bootstrap, the Reviewer source will be under:

`C:\GitHub\Chaturbate_Projects\projects\ctbrec-mobile-reviewer\app`

Run Codex from the umbrella repository root for cross-project work, or from the project/app folder for narrowly scoped Reviewer implementation.
