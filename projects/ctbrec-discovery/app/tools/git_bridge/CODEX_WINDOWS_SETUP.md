# Optional Codex CLI setup

The Discovery Git Bridge itself does **not** require Codex CLI. It requires Python, Git, GitHub CLI and (for JavaScript validation) Node.js.

If PowerShell reports that `npm.ps1` is blocked when installing Codex, use the Windows command shim instead:

```powershell
npm.cmd install -g @openai/codex@latest
```

Then reopen the terminal and run:

```powershell
codex --version
codex --login
```

The umbrella repo is normally:

`C:\GitHub\Chaturbate_Projects`

Discovery source is normally:

`C:\GitHub\Chaturbate_Projects\projects\ctbrec-discovery\app`
