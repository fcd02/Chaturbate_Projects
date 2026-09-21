from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
bridge = ROOT / 'tools' / 'git_bridge'
setup = (bridge / 'Setup-Git-Bridge.ps1').read_text(encoding='utf-8')
apply = (bridge / 'Apply-ChatGPT-Update.ps1').read_text(encoding='utf-8')
setup_cmd = (bridge / 'Setup-Git-Bridge.cmd').read_text(encoding='utf-8')
apply_cmd = (bridge / 'Apply-ChatGPT-Update.cmd').read_text(encoding='utf-8')

version_file = (bridge / 'BRIDGE_VERSION.txt').read_text(encoding='utf-8').strip()
assert version_file == '1.1.0'
assert '$BridgeVersion = "1.1.0"' in setup
assert '$BridgeVersion = "1.1.0"' in apply

# Field regression: Windows PowerShell 5.1 cannot bind the two-character string
# "\\\\" to System.Char in TrimStart/TrimEnd.  The bridge must never use that
# pattern again.  Relative-path stripping is regex-based instead.
for text in (setup, apply):
    assert ".TrimStart('\\\\','/')" not in text
    assert '.TrimStart("\\\\","/")' not in text
    assert ".TrimEnd('\\\\')" not in text
    assert '.TrimEnd("\\\\")' not in text
    assert "-replace '^[\\\\/]+',''" in text

assert 'function Get-RelativeChildPath' in setup
assert 'function Get-RelativeChildPath' in apply
assert 'Get-RelativeChildPath $Source $_.FullName' in setup
assert apply.count('Get-RelativeChildPath $PayloadRoot $_.FullName') >= 2

# Second field bug found during the same audit: PowerShell unwraps a one-element
# array returned by a function, so indexing [0] on "py" can yield character 'p'.
# Resolve the executable path as a scalar string and use it directly.
assert 'function Get-PythonCommand' in apply
assert 'return [string]$pyLauncher.Source' in apply
assert 'return [string]$python.Source' in apply
assert '(Get-PythonCommand)[0]' not in apply
assert apply.count('$py = Get-PythonCommand') >= 2

# Path equality must not use the same broken TrimEnd binding.
assert 'Normalize-ComparePath $runtime' in apply
assert 'Normalize-ComparePath $appRoot' in apply

# Setup/update should establish a repo-local identity automatically if gh auth
# succeeded but Git user.name/user.email were never configured on this PC.
for text in (setup, apply):
    assert 'function Ensure-GitIdentity' in text
    assert "users.noreply.github.com" in text
assert 'Ensure-GitIdentity $RepoPath' in setup
assert 'Ensure-GitIdentity $repo' in apply

# Public source safety: .git internals and local bridge config never get mirrored.
for text in (setup, apply):
    assert r"(\.git|recu_browser_profile" in text
    assert "git_bridge.local.json" in text
    assert r"(^|/)models/readme\.md$" in text

# CMD launchers deliberately bypass execution policy for this process only.
assert '-ExecutionPolicy Bypass' in setup_cmd
assert '-ExecutionPolicy Bypass' in apply_cmd

# Version/cache identity for this bridge-hotfix release.
server = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')
sw = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')
html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
assert 'CTBRecMobile/2.15.10' in server
assert 'ctbrec-shell-v21510' in sw
assert 'app.js?v=21510' in html and 'rapid.js?v=21510' in html and 'styles.css?v=21510' in html

print('PASS: v2.15.10 Git Bridge Windows compatibility — relative-path Char binding, Python resolver, path comparison, Git identity, and public-source guards')
