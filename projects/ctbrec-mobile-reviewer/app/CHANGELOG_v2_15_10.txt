CTBRec Mobile Reviewer v2.15.10

Git Bridge Windows PowerShell compatibility hotfix

Field failure fixed
- Setup-Git-Bridge.ps1 failed on Windows PowerShell while copying the public-safe source with: Cannot convert argument "trimChars" value "\\" for TrimStart to System.Char.
- Root cause: PowerShell does not use backslash as an escape character, so the bridge passed a two-character string where .NET expected Char values.

Changes
- Replaced TrimStart/TrimEnd separator stripping with regex-based path normalization in Setup and Apply paths.
- Added shared Get-RelativeChildPath helpers for setup copy, patch copy, and runtime backup.
- Replaced runtime/app path comparison with Normalize-ComparePath.
- Fixed the Python executable resolver so PowerShell cannot unwrap "py" and then index it to the single character "p".
- Added automatic repo-local Git user.name/user.email setup from the authenticated GitHub account when missing, using a GitHub noreply address.
- Public-source guard now excludes .git internals and git_bridge.local.json explicitly.
- models/README.md is allowed through the public-source filter while model binaries remain excluded.
- Added v2.15.10 Windows-compatibility regression coverage.

Preserved behavior
- No Reviewer sorting, mosaic, playback, READY, Recu, Keep Last, preview, selection, queue, or destructive-action logic changed.
- v2.15.9 seekable MP4 cache and fixed preview geometry are unchanged.
- Existing runtime/private state remains excluded from PATCH and Git mirroring.
